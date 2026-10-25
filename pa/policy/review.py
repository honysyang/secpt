"""计划结构检查（S12 约束 D）：纯函数，给定 TaskDraft[] 返回 Issue[]。

- 无 success_criteria → I4
- 预算 steps > THRESHOLDS["plan_budget_steps"] → I3
- scope_slice 为空 → I4（否则任务无边无际）
- 依赖指向 known_ids 之外的 id → I4（known_ids = 已有任务 ∪ 本批次
  将创建的模拟 id，由调用方 coach.on_plan 按 create_task 的分配规则算好）

Issue 带 draft_index，调用方据此在 enforce 下拦掉对应草案。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

from pa.models import TaskDraft
from pa.policy.thresholds import THRESHOLDS


@dataclass(frozen=True)
class Issue:
    level: str                    # I1~I5
    code: str                     # no_criteria | budget_too_large | empty_scope
                                  # | dangling_dep
    draft_index: int
    detail: dict = field(default_factory=dict)


def review_drafts(drafts: list[TaskDraft],
                  known_ids: FrozenSet[str] = frozenset()) -> list[Issue]:
    issues: list[Issue] = []
    for i, d in enumerate(drafts):
        if not d.success_criteria:
            issues.append(Issue("I4", "no_criteria", i,
                                {"goal": d.goal[:80]}))
        if d.budget.steps > THRESHOLDS["plan_budget_steps"]:
            issues.append(Issue("I3", "budget_too_large", i,
                                {"steps": d.budget.steps,
                                 "limit": THRESHOLDS["plan_budget_steps"]}))
        if not d.scope_slice:
            issues.append(Issue("I4", "empty_scope", i,
                                {"goal": d.goal[:80]}))
        for dep in d.deps:
            if dep not in known_ids:
                issues.append(Issue("I4", "dangling_dep", i,
                                    {"dep": dep, "goal": d.goal[:80]}))
    return issues


def batch_ids(start_seq: int, n: int) -> set[str]:
    """create_task 分配规则的模拟：从 start_seq 起 n 个草案将得到的 id
    （t-{seq:04d}），供依赖检查把本批次 id 视为合法引用。"""
    return {f"t-{start_seq + i + 1:04d}" for i in range(n)}
