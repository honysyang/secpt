"""行为统计重算（裁定 2）。

state.meta 里的五个行为参数是派生缓存，唯一真源是事件账本：restore() 用
本模块从全量事件重算；运行期不存在单独维护这些参数的写路径。任何重启、
删快照，重放后都必须得到同样的值。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pa.models import Event


def derive_stats(events: Iterable[Event]) -> dict[str, Any]:
    """从事件流重算五个行为参数，返回可直接并入 state.meta 的 dict。"""
    no_progress = 0
    saw_clue = False
    plan_rewrites = 0
    planned = 0
    virtual_success = 0
    worker_returns = 0
    failures_by_class: dict[str, int] = {}
    restarts: dict[str, int] = {}
    fp_attempts: dict[str, int] = {}
    for ev in events:
        t = ev.type
        if t == "clue.added":
            saw_clue = True
        elif t == "budget.charged":
            # 一个超级步结算一次 budget.charged：有新线索则清零，否则累加
            no_progress = 0 if saw_clue else no_progress + 1
            saw_clue = False
        elif t == "plan.created":
            planned += int(ev.payload.get("n", 0))
        elif t == "plan.revised":
            plan_rewrites += 1
        elif t == "action.finished":
            # §6.5.1 失败重试计数：非 cached 的失败执行才计数，
            # cached 命中（含 cache_hit_exhausted）只是审计回声，不计
            if not ev.payload.get("cached") and int(ev.payload.get("exit", 0)) != 0:
                fp = str(ev.payload.get("fp", ""))
                fp_attempts[fp] = fp_attempts.get(fp, 0) + 1
        elif t == "worker.returned":
            worker_returns += 1
            failure = ev.payload.get("failure")
            if failure:
                cls = str(failure.get("cls", "?"))
                failures_by_class[cls] = failures_by_class.get(cls, 0) + 1
        elif t == "task.state_changed":
            to = ev.payload.get("to")
            frm = ev.payload.get("from")
            tid = str(ev.payload.get("task_id", ""))
            if to == "ready" and frm in ("running", "failed"):
                restarts[tid] = restarts.get(tid, 0) + 1
            if to == "failed" and ev.payload.get("reason") == "virtual_success":
                virtual_success += 1
    return {
        "no_progress_steps": no_progress,
        "plan_rewrites": plan_rewrites,
        "restarts_same_task": max(restarts.values()) if restarts else 0,
        "virtual_success_count": virtual_success,   # vs_events：按事件计（教练用）
        "worker_returns": worker_returns,
        "failures_by_class": dict(sorted(failures_by_class.items())),
        "planned": planned,
        "fp_attempts": fp_attempts,
    }


def advisory_occurrences(events: Iterable[Event]) -> dict[str, int]:
    """1.4：同 issue+target 的命中次数（首次 mentor.advisory 记 occurrences，
    其后每条 mentor.advisory_agg{delta:1} 累加）。

    故意不进 state.meta：meta 任一键差都会让 off/shadow 两个 mission 的
    状态 dump 不同（S12 验收一要求 shadow 与 off 逐字节一致）。occurrences
    是教练的内部判据，谁要用谁从账本现算，口径唯一且可重放。"""
    occ: dict[str, int] = {}
    for ev in events:
        if ev.type == "mentor.advisory":
            key = f'{ev.payload.get("issue")}:{ev.payload.get("target")}'
            occ[key] = max(int(occ.get(key, 0)),
                           int(ev.payload.get("occurrences", 1)))
        elif ev.type == "mentor.advisory_agg":
            key = f'{ev.payload.get("issue")}:{ev.payload.get("target")}'
            occ[key] = int(occ.get(key, 0)) + int(ev.payload.get("delta", 1))
    return occ
