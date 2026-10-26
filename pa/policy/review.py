"""计划结构检查（S12 约束 D）：纯函数，给定 TaskDraft[] 返回 Issue[]。

- 无 success_criteria → I4
- 预算 steps > THRESHOLDS["plan_budget_steps"] → I3
- scope_slice 为空 → I4（否则任务无边无际）
- 依赖指向 known_ids 之外的 id → I4（known_ids = 已有任务 ∪ 本批次
  将创建的模拟 id，由调用方 coach.on_plan 按 create_task 的分配规则算好）

Issue 带 draft_index，调用方据此在 enforce 下拦掉对应草案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import FrozenSet

from pa.core.evidence_kinds import evidence_ok
from pa.models import TaskDraft
from pa.policy.thresholds import THRESHOLDS

# 覆盖度判据与 reflect._COVERAGE_RE 同口径；dns 是新增能力族。
_COVERAGE_RE = re.compile(r"^(probe|http|shell|tls|dns):(\d+)$")
# 历史/测试用的简单证据词（never-met、port.open 等）允许保留；
# 只硬拒绝散文和已知不可判定的伪证据。
_SIMPLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_BAD_EVIDENCE = frozenset({"shell.output", "fs.read"})


@dataclass(frozen=True)
class Issue:
    level: str  # I1~I5
    code: str  # no_criteria | budget_too_large | empty_scope
    # | dangling_dep | bad_criterion
    draft_index: int
    detail: dict = field(default_factory=dict)


def review_drafts(
    drafts: list[TaskDraft], known_ids: FrozenSet[str] = frozenset()
) -> list[Issue]:
    issues: list[Issue] = []
    for i, d in enumerate(drafts):
        if not d.success_criteria:
            issues.append(Issue("I4", "no_criteria", i, {"goal": d.goal[:80]}))
        bad = [c.evidence for c in d.success_criteria if not criterion_ok(c.evidence)]
        if bad:
            issues.append(
                Issue(
                    "I4",
                    "bad_criterion",
                    i,
                    {"goal": d.goal[:80], "criteria": bad},
                )
            )
        if d.budget.steps > THRESHOLDS["plan_budget_steps"]:
            issues.append(
                Issue(
                    "I3",
                    "budget_too_large",
                    i,
                    {"steps": d.budget.steps, "limit": THRESHOLDS["plan_budget_steps"]},
                )
            )
        if not d.scope_slice:
            issues.append(Issue("I4", "empty_scope", i, {"goal": d.goal[:80]}))
        for dep in d.deps:
            if dep not in known_ids:
                issues.append(
                    Issue("I4", "dangling_dep", i, {"dep": dep, "goal": d.goal[:80]})
                )
    return issues


def criterion_ok(evidence: str) -> bool:
    """任务判据必须是可判定证据：登记的证据类型或 族:条数 覆盖度词。

    拒绝散文（"给出 A 记录 IP 或明确的解析失败原因"）、拒绝未登记的
    形如 fs.read / shell.output 的伪证据词。这些判据在 criteria_met 里
    永远匹配不到，任务必然 virtual_success。
    """
    e = str(evidence or "").strip()
    if not e or e in _BAD_EVIDENCE:
        return False
    if _COVERAGE_RE.fullmatch(e):
        return True
    if evidence_ok(e):
        return True
    # 兼容历史账本/测试的简单 token；散文（空格/中文/标点）一律拒绝。
    return bool(_SIMPLE_TOKEN_RE.fullmatch(e))


def filter_valid_drafts(
    drafts: list[TaskDraft],
) -> tuple[list[TaskDraft], list[tuple[TaskDraft, list[str]]]]:
    """硬过滤不可判定判据的草案：返回 (保留, [(被拒草案, bad_criteria)])。

    这是 planner/护栏层的合同门，不依赖 coach 档位；shadow 模式也必须
    拦住不可执行任务，否则会进入 3 次重试的确定性失败循环。
    """
    kept: list[TaskDraft] = []
    rejected: list[tuple[TaskDraft, list[str]]] = []
    for d in drafts:
        if not d.success_criteria:
            # no_criteria 交给 coach 的 I4 检查（enforce 拦、shadow 只 advise），
            # 合同门不代替它做结构判断。
            kept.append(d)
            continue
        good = [c for c in d.success_criteria if criterion_ok(c.evidence)]
        bad = [c.evidence for c in d.success_criteria if not criterion_ok(c.evidence)]
        if not good:
            rejected.append((d, bad or [c.evidence for c in d.success_criteria]))
            continue
        if bad:
            # 保留可判定判据，丢弃散文/伪证据；任务目标本身仍由 goal 表达。
            d.success_criteria = good
            rejected.append((d, bad))
        kept.append(d)
    return kept, rejected


def batch_ids(start_seq: int, n: int) -> set[str]:
    """create_task 分配规则的模拟：从 start_seq 起 n 个草案将得到的 id
    （t-{seq:04d}），供依赖检查把本批次 id 视为合法引用。"""
    return {f"t-{start_seq + i + 1:04d}" for i in range(n)}
