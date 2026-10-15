"""启动时经验检索：objective 关键词打分 → top-k 注入 system prompt。

打分复用 ExperienceStore.search 的机制（标题 3 分/描述 2 分/指标 1.5 分），
但数据源是 learning/store.py 的 LearnedExperience（含 what_worked/pitfalls）。
SECPT_EXPERIENCE=0 关闭；默认 top-3。
"""

from __future__ import annotations

import os
import re
import time as _time

from secpt.knowledge.embedding import EMBED_MIN_SCORE as _SEM_MIN
from secpt.learning.store import ExperienceJournal, LearnedExperience

_MAX_ITEMS = 3
_MAX_ITEM_CHARS = 220


_HALF_LIFE_DAYS = 21.0  # 经验半衰期：3 周前的经验权重减半

# ⑥ 相关性门槛：指标术语最短长度——1-2 字符的术语（如 "SP"）纯子串匹配噪声极大
_MIN_TERM_LEN = 3


def _term_hit(term: str, q: str) -> bool:
    """术语是否在查询中命中：ASCII 按词边界匹配（防 'sp' 误中 'resp'/'response'），
    中文按子串匹配（无词边界概念）。过短术语一律不参与（噪声源）。"""
    t = (term or "").strip().lower()
    if len(t) < _MIN_TERM_LEN:
        return False
    if t.isascii():
        # 词边界：两侧不得紧邻 [a-z0-9_]，否则视为更长单词的一部分
        return re.search(rf"(?<![a-z0-9_]){re.escape(t)}(?![a-z0-9_])", q) is not None
    return t in q


# 标题/方法字段里不参与命中的 ASCII 填充词——命中它们与任务无关，只会放大噪声
_ASCII_FILLER = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "not",
    "but",
    "are",
    "was",
    "can",
    "you",
    "via",
    "use",
    "using",
    "get",
    "when",
    "how",
    "why",
}


def _field_hit(text: str, q: str) -> bool:
    """经验字段（标题/有效方法）是否命中查询。

    方向必须是"字段的内容出现在查询里"：查询是整条 objective（长），字段是
    蒸馏产物（短）。旧实现 `q in exp.title` 方向反了——长查询包含于短标题
    永不成立，3/2 分强信号分支是死代码，召回全靠 indicators/technique 兜底。
    判定：整短语包含，或字段内显著词命中（ASCII ≥3 字符按词边界、连排汉字
    按子串，均复用 _term_hit 口径；填充词不参与）。
    """
    t = (text or "").strip().lower()
    if not t:
        return False  # 空字段恒不命中（"" in q 恒真，必须前置拦截）
    if t in q:
        return True
    for word in re.findall(r"[a-z][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", t):
        if word in _ASCII_FILLER:
            continue
        if _term_hit(word, q):
            return True
    return False


def _recency_factor(ts: float) -> float:
    """时效衰减：exp(-ln2 * age/半衰期)——老经验不消失但降权。"""
    if not ts:
        return 1.0
    age_days = max(0.0, (_time.time() - ts) / 86400.0)
    import math

    return math.exp(-math.log(2) * age_days / _HALF_LIFE_DAYS)


def _exp_doc(e: LearnedExperience) -> str:
    """经验侧语义文档：indicators 是蒸馏产物里天然的语义锚点。"""
    parts = [e.title, e.technique, e.what_worked, *e.indicators]
    return " ".join(p for p in parts if p)


def _semantic_scores(query: str, exps: list[LearnedExperience]) -> dict[str, float] | None:
    """{exp_id: cosine}；嵌入不可用返回 None（调用方降级为纯字面打分）。

    best-effort：任何异常（含提供者不可用）都静默返回 None——检索绝不抛。
    """
    if not exps or not query:
        return None
    try:
        from secpt.knowledge import embedding as _emb

        vecs = _emb.embed_cached([query] + [_exp_doc(e) for e in exps])
    except Exception:
        return None
    if vecs is None:
        return None
    return {e.id: _emb.cosine(vecs[0], v) for e, v in zip(exps, vecs[1:], strict=True)}


def _usage_factor(hits: int, misses: int) -> float:
    """历史命中率因子（C 反馈闭环）：0.7（全挂）~1.3（全中），小样本向 1.0 收缩。

    语义：注入该经验的任务最终成败（粗粒度归因——outcome 写回在 solve 收尾，
    一条经验同时服务多个子 job 时按任务总结果计）。importance 是蒸馏时 LLM
    的静态自评；本因子是运行史的动态修正，两者相乘。
    """
    n = hits + misses
    if n <= 0:
        return 1.0
    factor = 0.7 + 0.6 * (hits / n)
    shrink = n / (n + 3.0)  # n=1 时只取 25% 偏移，n=10 取 77%，防小样本误杀
    return 1.0 + (factor - 1.0) * shrink


def _score(
    query: str,
    exp: LearnedExperience,
    usage: dict[str, tuple[int, int]] | None = None,
    sem: float | None = None,
) -> tuple[float, bool]:
    """三维打分 → (相关性分, 是否强信号)。

    强信号 = 标题/机理/技术名命中（跨任务类型亦可信，说明确属同类问题）；
    弱信号 = 仅指标术语命中（受词边界与最短长度约束，跨任务类型不足采信）。
    usage：{exp_id: (hits, misses)}——历史命中率因子（C 反馈闭环）。
    sem：语义相似度（B 语义通路）——模糊信号，只加权不置强（跨域不采信）。
    """
    q = (query or "").lower()
    if not q:
        return 0.0, False
    s = 0.0
    strong = False
    if _field_hit(exp.title, q):
        s += 3.0
        strong = True
    if _field_hit(exp.what_worked, q):
        s += 2.0
        strong = True
    for ind in exp.indicators:
        if _term_hit(ind, q):
            s += 1.5
    if exp.technique and _term_hit(exp.technique, q):
        s += 2.0
        strong = True
    # B 语义通路：与技术名命中同级贡献（2 分×相似度），低于阈值不算命中
    if sem is not None and sem >= _SEM_MIN:
        s += 2.0 * sem
    if s <= 0:
        return 0.0, False
    importance_w = 0.5 + (exp.importance or 5.0) / 10.0  # 0.6 ~ 1.5
    h, m = (usage or {}).get(exp.id, (0, 0))
    return s * importance_w * _recency_factor(exp.ts) * _usage_factor(h, m), strong


def search_experiences(
    query: str,
    limit: int = _MAX_ITEMS,
    journal: ExperienceJournal | None = None,
    *,
    task_type: str = "",
) -> list[LearnedExperience]:
    """按 objective 检索最相关的经验。

    ⑥ 相关性门槛：跨任务类型（如把 ctf 经验注入 benchmark 任务）只采信强信号——
    纯指标命中抗噪差（实测 ctf 的 "SP" 指标曾误中 benchmark 模板里的 resp/response，
    导致无关经验污染 17 个子任务的 context）。task_type 为空时不做跨域限制（兼容旧调用）。
    """
    if os.environ.get("SECPT_EXPERIENCE") == "0":
        return []
    jr = journal or ExperienceJournal()
    usage = jr.load_usage()  # C 反馈闭环：历史命中率参与排序
    exps = jr.load()
    sem = _semantic_scores(query, exps)  # B 语义通路（None=降级纯字面）
    out: list[tuple[float, LearnedExperience]] = []
    for e in exps:
        s, strong = _score(query, e, usage, sem=(sem.get(e.id) if sem else None))
        if s <= 0:
            continue
        if (
            task_type
            and e.task_type
            and e.task_type != "generic"
            and e.task_type != task_type
            and not strong
        ):
            continue  # 跨任务类型且只有弱信号 → 不注入（宁缺毋滥）
        out.append((s, e))
    out.sort(key=lambda x: -x[0])
    return [e for _, e in out[:limit]]


def experience_section(
    query: str,
    journal: ExperienceJournal | None = None,
    *,
    task_type: str = "",
) -> str:
    """渲染注入 system_extra 的【历史经验】段；无匹配返回空串。"""
    exps = search_experiences(query, journal=journal, task_type=task_type)
    if not exps:
        return ""
    lines = ["【历史经验（同类任务沉淀，可参考但不要照抄）】"]
    for e in exps:
        item = f"- {e.title}"
        if e.what_worked:
            item += f"：{e.what_worked[:_MAX_ITEM_CHARS]}"
        if e.pitfalls:
            item += "；已排除：" + "、".join(p[:40] for p in e.pitfalls[:3])
        lines.append(item[: _MAX_ITEM_CHARS + 80])
    return "\n".join(lines)
