"""启动时经验检索：objective 关键词打分 → top-k 注入 system prompt。

打分复用 ExperienceStore.search 的机制（标题 3 分/描述 2 分/指标 1.5 分），
但数据源是 learning/store.py 的 LearnedExperience（含 what_worked/pitfalls）。
SECPT_EXPERIENCE=0 关闭；默认 top-3。
"""

from __future__ import annotations

import os
import re
import time as _time

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


def _recency_factor(ts: float) -> float:
    """时效衰减：exp(-ln2 * age/半衰期)——老经验不消失但降权。"""
    if not ts:
        return 1.0
    age_days = max(0.0, (_time.time() - ts) / 86400.0)
    import math

    return math.exp(-math.log(2) * age_days / _HALF_LIFE_DAYS)


def _score(query: str, exp: LearnedExperience) -> tuple[float, bool]:
    """三维打分 → (相关性分, 是否强信号)。

    强信号 = 标题/机理/技术名命中（跨任务类型亦可信，说明确属同类问题）；
    弱信号 = 仅指标术语命中（受词边界与最短长度约束，跨任务类型不足采信）。
    """
    q = (query or "").lower()
    if not q:
        return 0.0, False
    s = 0.0
    strong = False
    if q in exp.title.lower():
        s += 3.0
        strong = True
    if exp.what_worked and q in exp.what_worked.lower():
        s += 2.0
        strong = True
    for ind in exp.indicators:
        if _term_hit(ind, q):
            s += 1.5
    if exp.technique and _term_hit(exp.technique, q):
        s += 2.0
        strong = True
    if s <= 0:
        return 0.0, False
    importance_w = 0.5 + (exp.importance or 5.0) / 10.0  # 0.6 ~ 1.5
    return s * importance_w * _recency_factor(exp.ts), strong


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
    out: list[tuple[float, LearnedExperience]] = []
    for e in jr.load():
        s, strong = _score(query, e)
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
