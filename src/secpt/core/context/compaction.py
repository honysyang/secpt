"""S3 上下文压缩 Compaction。

触发：est_messages_tokens >= 0.72 * window（0.72：实测 0.55 时压缩过频——
     68 步触发 21 次、每次仅删 4 条，摘要 LLM 调用浪费；提高阈值后每次压缩
     批量删到 0.5×window 以下，次数大幅下降。append-only 命中 prefix cache
     更便宜，压缩会打破命中，故不追求更低阈值）。

保护：
- 首条 system 消息
- 最新 PROTECT_LATEST 条消息
- 已记录 finding/credential（存在 store/events，天然不在 messages 里被删，
  且摘要 prompt 会要求模型把凭据写进摘要）
- "摘要生成中"占位（删了回填落空）

可删单元（E 修复后两类）：
- 完整组 (assistant[含 tool_calls] + 它的全部 tool messages)
    ⚠️ 只删 assistant 不删 tool result（或反过来）都会导致 API 400
- 独立 user 消息（nudge/预算警告/旧摘要占位）——此前永不删除、单调堆积；
  其正文随 removed_msgs 进摘要，信息不悬空丢失
产物：在被删区间原位插入 {"role":"user","content":"【历史摘要 #k】\n<六段摘要>"}

红线：本模块（Compaction）是除 Rollover 之外唯一允许删改 messages 的地方。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from secpt.core.context.estimator import est_messages_tokens

COMPACT_THRESHOLD = 0.72
COMPACT_TARGET = 0.5  # 批量删除目标：一次压到 0.5×window 以下（少次多删）
PROTECT_LATEST = 6  # 尾部保护条数（12 时大输出场景保护尾本身超阈值，压缩永远压不下去）
MAX_GROUPS_PER_COMPACT = 200  # 单次压缩最多删组数（安全上限）


def should_compact(messages: list[dict], window: int) -> bool:
    """是否触发压缩。"""
    if window <= 0:
        return False
    return est_messages_tokens(messages) >= COMPACT_THRESHOLD * window


def _call_ids(msg: dict) -> set[str]:
    return {tc.get("id") for tc in (msg.get("tool_calls") or [])}


def group_end(messages: list[dict], start: int) -> int:
    """返回 messages[start] 所在组的结束下标（含）。

    组 = assistant（含 tool_calls）+ 它的全部 tool messages（连续）。
    无 tool_calls 的 assistant 组就是它自己。
    """
    needed = _call_ids(messages[start])
    if not needed:
        return start
    j = start
    while j + 1 < len(messages):
        nxt = messages[j + 1]
        if nxt.get("role") == "tool" and nxt.get("tool_call_id") in needed:
            j += 1
            needed.discard(nxt.get("tool_call_id"))
            if not needed:
                break
        else:
            break
    return j


def _is_complete_group(messages: list[dict], start: int) -> bool:
    """该组是否完整（assistant 的全部 tool_call 都有对应 tool 结果）。"""
    needed = _call_ids(messages[start])
    if not needed:
        return True
    have = 0
    for x in messages[start + 1 :]:
        if x.get("role") == "tool" and x.get("tool_call_id") in needed:
            have += 1
    return have >= len(needed)


def _protected_start(messages: list[dict]) -> int:
    """可删区间的起始下标：首条 system 之后。"""
    lo = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            lo = i + 1
            break
    return lo


def _next_removable(messages: list[dict], lo: int, protected_tail: int) -> tuple[int, int] | None:
    """在 [lo, len-protected_tail) 内找第一个可删单元，返回 (start, end)；无则 None。

    可删单元两类（E 修复）：
    - assistant 整组（含 tool_calls 与全部 tool 结果，必须完整配对）
    - 独立 user 消息（nudge/预算警告/旧摘要占位等——此前永不删除，
      多次压缩后在链里单调堆积、只能靠 rollover 清场；它们的正文会随
      removed_msgs 进入摘要，信息以六段形式保留，不悬空丢失）
    未回填的"摘要生成中"占位不删：删了回填就永远找不到落点（pending 摘要落空）。
    """
    end_limit = max(lo, len(messages) - protected_tail)
    i = lo
    while i < end_limit:
        m = messages[i]
        if m.get("role") == "assistant" and _is_complete_group(messages, i):
            return i, group_end(messages, i)
        if m.get("role") == "user" and _is_removable_user(m):
            return i, i
        i += 1
    return None


def _is_removable_user(m: dict) -> bool:
    """独立 user 消息是否可删：除"摘要生成中"占位外均可（见 _next_removable）。"""
    return PLACEHOLDER_MARK not in str(m.get("content") or "")


def plan_compaction(
    messages: list[dict], window: int, *, protected_tail: int = PROTECT_LATEST
) -> tuple[list[dict], list[dict], int] | None:
    """纯同步规划一次压缩：算出「删哪些、删到哪」但不执行摘要。

    返回 (删除后的 messages, removed_msgs, 摘要插入下标)；无需压缩返回 None。
    摘要生成（LLM 调用）与插桩由调用方接续——这是非阻塞 compaction 的拆分点：
    主循环只做删除+占位（微秒级），摘要留给后台 worker。
    """
    if not should_compact(messages, window):
        return None

    remaining = list(messages)
    lo = _protected_start(remaining)
    removed_msgs: list[dict] = []
    first_idx: int | None = None

    # 批量删除：目标 0.5×window（一次压到位，避免每步触发压缩）
    target = COMPACT_TARGET * window
    while est_messages_tokens(remaining) >= target:
        hit = _next_removable(remaining, lo, protected_tail)
        if hit is None:
            break
        idx, end = hit
        if first_idx is None:
            first_idx = idx
        removed_msgs += remaining[idx : end + 1]
        del remaining[idx : end + 1]
        # lo 若落在被删区间之后，回退到被删位置继续向前找
        if lo > idx:
            lo = idx
        if len(removed_msgs) > MAX_GROUPS_PER_COMPACT:
            break

    if not removed_msgs:
        return None
    return remaining, removed_msgs, first_idx if first_idx is not None else 0


async def compact_messages(
    messages: list[dict],
    objective: str,
    summarizer,
    window: int,
    *,
    k: int = 0,
    protected_tail: int = PROTECT_LATEST,
):
    """压缩一次：删最老整组直到 token 回落，原位插一条六段摘要。

    返回 (新 messages, 删除条数, 摘要文本)。removed==0 表示未发生压缩。
    阻塞式旧路径：摘要内联 await（engine 主循环已改走 plan_compaction + 后台
    worker，本函数保留给独立调用方与既有测试）。
    """
    plan = plan_compaction(messages, window, protected_tail=protected_tail)
    if plan is None:
        return messages, 0, ""

    remaining, removed_msgs, first_idx = plan
    summary = await summarizer(objective, removed_msgs)
    summary_msg = {"role": "user", "content": f"【历史摘要 #{k}】\n{summary}"}
    insert_at = min(first_idx, len(remaining))
    remaining.insert(insert_at, summary_msg)
    return remaining, len(removed_msgs), summary


# ---------------------------------------------------------------------------
# 非阻塞 compaction：file-backed 请求队列 + 占位摘要 + 后台补全
#
# 主循环（同步、微秒级）：plan_compaction 删组 → 原位插占位摘要 → 落请求文件
#   → 发 compaction_requested 事件 → 唤醒后台 worker。
# 后台 worker（不阻塞主循环）：拉请求 → LLM 六段摘要 → 写 <k>.summary.md
#   → 发 compaction_completed 事件；主循环下一轮把占位替换为真摘要。
# 引擎崩溃时残留请求由 resume 后的下一次 run 或 `secpt worker summarize` 兜底。
# ---------------------------------------------------------------------------

PLACEHOLDER_MARK = "（摘要生成中"  # 占位摘要识别标记（swap 用）


def placeholder_content(k: int) -> str:
    """占位摘要正文：删掉的明细已不在链里，先给模型一句交代。"""
    return (
        f"【历史摘要 #{k}】{PLACEHOLDER_MARK}：本段最早的一批对话明细已按整组删除，"
        "六段式摘要正由后台生成、稍后回填到这里。请先基于剩余上下文继续任务。"
    )


def compaction_dir(job_id: str, jobs_dir: Path) -> Path:
    return Path(jobs_dir) / job_id / "compaction"


def write_compaction_request(
    job_id: str,
    jobs_dir: Path,
    *,
    k: int,
    window: int,
    objective: str,
    removed_msgs: list[dict],
) -> Path:
    """落一个摘要请求到 pending 队列（原子写）。文件即任务，worker 拉取处理。"""
    d = compaction_dir(job_id, jobs_dir) / "pending"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "k": k,
        "window": window,
        "removed_n": len(removed_msgs),
        "objective": objective,
        "removed_msgs": removed_msgs,
    }
    path = d / f"{k:03d}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def read_pending_requests(jobs_dir: Path, job_id: str) -> list[tuple[Path, dict]]:
    """拉取全部待处理请求（按 k 升序——先到先摘要）。"""
    d = compaction_dir(job_id, jobs_dir) / "pending"
    if not d.is_dir():
        return []
    out: list[tuple[Path, dict]] = []
    for p in sorted(d.glob("[0-9][0-9][0-9].json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue  # 半截文件（断电窗口）跳过，下次再试
    return out


def summary_path(jobs_dir: Path, job_id: str, k: int) -> Path:
    """第 k 次压缩的摘要文件（完成真相源：文件存在=已生成）。"""
    return compaction_dir(job_id, jobs_dir) / f"{k:03d}.summary.md"


def load_summary(jobs_dir: Path, job_id: str, k: int) -> str | None:
    p = summary_path(jobs_dir, job_id, k)
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8")


def complete_request(path: Path) -> None:
    """请求处理完毕：摘除 pending 文件（summary 文件才是完成真相源）。"""
    path.unlink(missing_ok=True)


def swap_summary_placeholders(
    messages: list[dict], jobs_dir: Path, job_id: str, pending_ks: set[int]
) -> int:
    """把已生成摘要回填到链里的占位消息（主循环单线程调用，无并发竞态）。

    返回回填条数；未完成的 k 留在 pending_ks 下轮再试。
    """
    swapped = 0
    for k in sorted(pending_ks):
        s = load_summary(jobs_dir, job_id, k)
        if not s:
            continue
        prefix = f"【历史摘要 #{k}】"
        for m in messages:
            if m.get("role") == "user" and str(m.get("content", "")).startswith(prefix):
                m["content"] = f"{prefix}\n{s}"
                break
        pending_ks.discard(k)
        swapped += 1
    return swapped


async def complete_request_summary(path: Path, req: dict, summarizer, sink) -> bool:
    """后台完成一个摘要请求：LLM 摘要 → 写 <k>.summary.md → 发完成事件。

    sink 为 duck-typing（EventSink 或测试桩），事件失败不阻断补全。
    摘要连续失败时写入失败占位摘要并照常完成，防 pending 堆积空转。
    """
    k = int(req.get("k") or 0)
    window = int(req.get("window") or 0)
    removed_n = int(req.get("removed_n") or 0)
    objective = str(req.get("objective") or "")
    removed_msgs = list(req.get("removed_msgs") or [])

    summary = None
    last_err: Exception | None = None
    for _attempt in range(3):
        try:
            summary = await summarizer(objective, removed_msgs)
            break
        except Exception as e:  # LLM 网络抖动重试
            last_err = e
            await asyncio.sleep(1.0 * (_attempt + 1))
    if summary is None:
        summary = f"（摘要生成失败：{last_err}；明细已删除，可从 events.jsonl 重放）"

    sp = path.parents[1] / f"{k:03d}.summary.md"  # path=<base>/<job>/compaction/pending/NNN.json
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_suffix(".tmp")
    tmp.write_text(summary, encoding="utf-8")
    tmp.replace(sp)
    complete_request(path)

    if sink is not None:
        with contextlib.suppress(Exception):
            sink.on_compaction_completed(k, summary, removed_n, window)
        with contextlib.suppress(Exception):
            sink.on_compaction(k, summary, removed_n, window)  # watch 兼容（legacy 类型）
    return True
