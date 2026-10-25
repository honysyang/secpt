"""事件投影：apply_event 纯函数（BUILD_SPEC §5.4）。

只读 ev、只改 state，无副作用、无 IO——恢复与正常运行走同一条代码路径，
这是可回放的前提（§5.3 约束）。投影表之外的类型只落账本，不参与状态重建
（ARCHITECTURE §5.3）。
"""
from __future__ import annotations

from pa.models import (
    Adjudication, Clue, Event, FindingProposal, Lease, MissionState, Task,
)


def apply_event(state: MissionState, ev: Event) -> None:
    t = ev.type
    if t == "task.created":
        task = Task.model_validate(ev.payload["task"])
        state.tasks[task.id] = task
        state.task_seq = max(state.task_seq, int(ev.payload.get("task_seq", 0)))
    elif t == "task.state_changed":
        task = state.tasks.get(ev.payload.get("task_id"))
        if task is not None:
            task.state = ev.payload["to"]
            # 事件携带 attempt / lease / reason（graph.transition 写入），
            # 重放据此还原，保证 live 与 restore 后逐字段一致；无 lease 一律清空
            if ev.payload.get("attempt") is not None:
                task.attempt = int(ev.payload["attempt"])
            lease = ev.payload.get("lease")
            task.lease = Lease.model_validate(lease) if lease else None
            if "reason" in ev.payload:
                task.block_reason = ev.payload["reason"]
    elif t == "clue.added":
        state.clues.append(Clue.model_validate(ev.payload["clue"]))
        state.clue_seq += 1
    elif t == "finding.proposed":
        state.findings.append(FindingProposal.model_validate(ev.payload["finding"]))
    elif t == "finding.adjudicated":
        adj = Adjudication.model_validate(ev.payload)
        state.adjudications[adj.finding_id] = adj
    elif t == "budget.charged":
        state.spent_tokens += int(ev.payload.get("tokens", 0))
        state.spent_seconds += int(ev.payload.get("seconds", 0))
        state.step += 1               # 一超级步一结算：step 由账本可重放
    elif t == "plan.created":
        if "strategy" in ev.payload:
            state.current_strategy = dict(ev.payload["strategy"])
    elif t == "plan.revised":
        _apply_plan_ops(state, ev.payload.get("ops", []))
    elif t == "mission.finished":
        state.finished = True
        if "summary" in ev.payload:
            state.finish_summary = str(ev.payload["summary"])
        if "reason" in ev.payload:
            state.finish_reason = str(ev.payload["reason"])
        if "plateau_reason" in ev.payload:
            state.meta["plateau_reason"] = str(ev.payload["plateau_reason"])


def _apply_plan_ops(state: MissionState, ops: list[dict]) -> None:
    for op in ops:
        kind = op.get("op")
        target = op.get("target")
        if kind == "add_task" and op.get("task") is not None:
            task = Task.model_validate(op["task"])
            state.tasks[task.id] = task
        elif kind in ("drop_task", "supersede") and target in state.tasks:
            # 旧任务不删，标 superseded（CONTROL_PLANE §6.7 任务图治理）
            t = state.tasks[target]
            t.state = "superseded"
            t.lease = None
        elif kind == "change_priority" and target in state.tasks \
                and op.get("priority") is not None:
            state.tasks[target].priority = float(op["priority"])
