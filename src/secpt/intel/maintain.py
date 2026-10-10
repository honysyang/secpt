"""冷节点压缩（图记忆派：遗忘由图结构判决，不是线性截断）。

ARTEX cold-graph 的可迁移最小实现：
1. 纯 SQL 判冷热：超 TTL 且**没有任何边连接到新节点**的 fact/hint = 冷
   （图上已孤立的旧观察——它们不再参与任何活利用链）
2. 分组后按内容算 SHA-256 签名 → 签名不变就跳过 LLM（缓存即省钱）
3. LLM 只负责把一组冷节点折叠成一条【冷摘要】fact；原节点标记 cold，
   recall 默认排除——记忆按关联取回，冷的不挡路也不丢
"""

from __future__ import annotations

import hashlib
import time

from secpt.intel.store import COLD_TTL_S, IntelStore


def _signature(group: list[dict]) -> str:
    raw = "|".join(sorted(str(g["id"]) for g in group))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def maintain(
    store: IntelStore | None = None,
    *,
    llm=None,
    model: str = "deepseek-chat",
    ttl_s: float = COLD_TTL_S,
    min_group: int = 3,
) -> dict:
    """跑一次冷压缩。返回 {candidates, compressed, cached, skipped}。

    best-effort：无 llm/无候选直接返回（维护绝不影响主链路）。
    """
    st = store or IntelStore()
    cands = st.cold_candidates(ttl_s=ttl_s)
    if not cands:
        return {"candidates": 0, "compressed": 0, "cached": 0, "skipped": 0}

    # 按月分组（时间邻近的观察主题相关，摘要更 coherent）
    groups: dict[str, list[dict]] = {}
    for n in cands:
        key = time.strftime("%Y-%m", time.localtime(n["ts"]))
        groups.setdefault(key, []).append(n)

    compressed = cached = skipped = 0
    for group in groups.values():
        if len(group) < min_group:
            skipped += len(group)  # 太小的组不值得动 LLM
            continue
        sig = _signature(group)
        existing = st.digest_cached(sig)
        if existing is not None:
            cached += len(group)  # 签名没变 → 复用旧摘要（零成本）
            st.mark_cold([g["id"] for g in group])
            continue
        summary = await _summarize(group, llm=llm, model=model)
        if not summary:
            skipped += len(group)
            continue
        sid = st.add_node("fact", f"【冷摘要 {sig}】{summary}", job_id="cold-maintain")
        st.record_digest(sig, sid)
        st.mark_cold([g["id"] for g in group])
        compressed += len(group)
    return {
        "candidates": len(cands),
        "compressed": compressed,
        "cached": cached,
        "skipped": skipped,
    }


async def _summarize(group: list[dict], *, llm=None, model: str) -> str:
    if llm is None:
        return ""
    items = "\n".join(f"- {g['content'][:200]}" for g in group[:30])
    prompt = f"""把下面 {len(group)} 条渗透观察（同一时期的旧记忆）折叠成一段摘要：
保留可复用的结论与踩坑，去掉一次性细节（具体 IP、随机 token 等）。200 字内。

{items}"""
    try:
        if hasattr(llm, "call"):
            resp = await llm.call([{"role": "user", "content": prompt}], [], model)
        else:
            resp = await llm([{"role": "user", "content": prompt}], [], model, "")
        return (resp.choices[0].message.content or "").strip()[:600]
    except Exception:
        return ""
