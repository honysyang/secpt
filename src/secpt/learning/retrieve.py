"""启动时经验检索：objective 关键词打分 → top-k 注入 system prompt。

打分复用 ExperienceStore.search 的机制（标题 3 分/描述 2 分/指标 1.5 分），
但数据源是 learning/store.py 的 LearnedExperience（含 what_worked/pitfalls）。
SECPT_EXPERIENCE=0 关闭；默认 top-3。
"""

from __future__ import annotations

import os
import time as _time

from secpt.learning.store import ExperienceJournal, LearnedExperience

_MAX_ITEMS = 3
_MAX_ITEM_CHARS = 220


_HALF_LIFE_DAYS = 21.0  # 经验半衰期：3 周前的经验权重减半


def _recency_factor(ts: float) -> float:
    """时效衰减：exp(-ln2 * age/半衰期)——老经验不消失但降权。"""
    if not ts:
        return 1.0
    age_days = max(0.0, (_time.time() - ts) / 86400.0)
    import math

    return math.exp(-math.log(2) * age_days / _HALF_LIFE_DAYS)


def _score(query: str, exp: LearnedExperience) -> float:
    """三维打分：相关性 × 重要性 × 时效（Generative Agents 三因子检索）。"""
    q = query.lower()
    s = 0.0
    if q in exp.title.lower():
        s += 3.0
    if q in exp.what_worked.lower():
        s += 2.0
    for ind in exp.indicators:
        if ind.lower() in q or q in ind.lower():
            s += 1.5
    if exp.technique and exp.technique.lower() in q:
        s += 2.0
    if s <= 0:
        return 0.0
    importance_w = 0.5 + (exp.importance or 5.0) / 10.0  # 0.6 ~ 1.5
    return s * importance_w * _recency_factor(exp.ts)


def search_experiences(
    query: str, limit: int = _MAX_ITEMS, journal: ExperienceJournal | None = None
) -> list[LearnedExperience]:
    """按 objective 检索最相关的经验。"""
    if os.environ.get("SECPT_EXPERIENCE") == "0":
        return []
    jr = journal or ExperienceJournal()
    scored = [(_score(query, e), e) for e in jr.load()]
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:limit]]


def experience_section(query: str, journal: ExperienceJournal | None = None) -> str:
    """渲染注入 system_extra 的【历史经验】段；无匹配返回空串。"""
    exps = search_experiences(query, journal=journal)
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
