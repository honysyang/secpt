"""教练编排（S12 约束 A/C）：三档模式 PA_MENTOR_MODE=off|shadow|enforce，
默认 shadow。

- off：零 mentor 事件；
- shadow：判定照旧，只落 mentor.advisory（不投影状态——shadow 与 off
  跑出的任务状态逐字节一致，验收一）；
- enforce：advisory + intervene 的三个动作（supersede / 提前收口 / 降级）。

advisory payload（约束 C）：level / issue / metric{实际值} /
threshold{当前阈值} / would_do / target / converged_after（null，事后回填）。
同一 issue+target 全 mission 只 advise 一次（幂等键）。
"""
from __future__ import annotations

import os

from pa.models import MissionState, TaskDraft
from pa.policy import intervene, review, signals
from pa.policy.thresholds import THRESHOLDS
from pa.store.writer import StoreWriter

_MODES = ("off", "shadow", "enforce")
_I4_CODES = ("no_criteria", "empty_scope", "dangling_dep")


def mode() -> str:
    m = os.environ.get("PA_MENTOR_MODE", "shadow").lower()
    return m if m in _MODES else "shadow"


def _advisory(writer: StoreWriter, *, level: str, issue: str,
              metric: dict, threshold: dict, would_do: str,
              target: str) -> None:
    writer.append("mentor.advisory", "mentor",
                  {"level": level, "issue": issue, "metric": metric,
                   "threshold": threshold, "would_do": would_do,
                   "target": target, "converged_after": None},
                  f"adv:{issue}:{target}")


def on_plan(state: MissionState, writer: StoreWriter,
            drafts: list[TaskDraft]) -> list[TaskDraft]:
    """bootstrap 时调用（planner 与 create_task 之间）。

    返回允许创建的草案：off/shadow 全量；enforce 拦掉带 I4 缺陷的
    （I3 无对应动作，只 advise 不拦——约束 F 不实现 I3 提醒）。
    """
    m = mode()
    if m == "off":
        return list(drafts)
    known = set(state.tasks) | review.batch_ids(state.task_seq, len(drafts))
    issues = review.review_drafts(drafts, frozenset(known))
    blocked_idx: set[int] = set()
    for iss in issues:
        if iss.code == "budget_too_large":
            _advisory(writer, level="I3", issue=iss.code,
                      metric=dict(iss.detail),
                      threshold={"plan_budget_steps":
                                 THRESHOLDS["plan_budget_steps"]},
                      would_do="", target="plan")
            continue
        _advisory(writer, level=iss.level, issue=iss.code,
                  metric=dict(iss.detail),
                  threshold={}, would_do="block_task",
                  target=f"plan#{iss.draft_index}")
        if m == "enforce":
            blocked_idx.add(iss.draft_index)
    return [d for i, d in enumerate(drafts) if i not in blocked_idx]


def on_round(state: MissionState, writer: StoreWriter) -> None:
    """scheduler 每轮末尾调用：三个信号，越线 advise；enforce 追加动作。"""
    m = mode()
    if m == "off":
        return
    meta = state.meta
    # 信号一：thrashing → supersede 最高尝试次数的任务
    ta = signals.thrashing(state)
    if ta >= THRESHOLDS["thrashing_attempts"]:
        target = sorted((t for t in state.tasks.values()
                         if t.attempt == ta), key=lambda t: t.id)[0]
        _advisory(writer, level="I2", issue="thrashing",
                  metric={"max_attempt": ta},
                  threshold={"attempts": THRESHOLDS["thrashing_attempts"]},
                  would_do="supersede", target=target.id)
        if m == "enforce":
            intervene.supersede_task(state, writer, target, "thrashing",
                                     {"max_attempt": ta})
    # 信号二：vs_rate → 降级 model（事件口径：vs_events / worker_returns）
    vr = signals.vs_rate(meta)
    if vr > THRESHOLDS["vs_rate"]:
        metric = {"vs_events": int(meta.get("virtual_success_count", 0)),
                  "worker_returns": int(meta.get("worker_returns", 0)),
                  "vs_rate": round(vr, 4)}
        _advisory(writer, level="I1", issue="vs_rate", metric=metric,
                  threshold={"vs_rate": THRESHOLDS["vs_rate"]},
                  would_do="degrade_model", target="model")
        if m == "enforce":
            import os as _os
            fallback = _os.environ.get("SECPT_FALLBACK_MODEL", "fallback-model")
            intervene.degrade_model(state, writer, fallback, metric)
    # 信号三：authz_ratio → 提前收口（多数失败是越界时计划已失真）
    ar = signals.authz_ratio(meta)
    if ar > THRESHOLDS["authz_ratio"]:
        _advisory(writer, level="I2", issue="authz_ratio",
                  metric={"authz_ratio": round(ar, 4),
                          "failures_by_class":
                          dict(meta.get("failures_by_class", {}))},
                  threshold={"authz_ratio": THRESHOLDS["authz_ratio"]},
                  would_do="settle_plateau", target="mission")
        if m == "enforce":
            satisfied = [a.id for a in state.spec.acceptance
                         if any(a.evidence in (c.key, str(c.value))
                                for c in state.clues)]
            intervene.settle_plateau(state, writer, satisfied)
