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
        "virtual_success_count": virtual_success,
        "planned": planned,
        "fp_attempts": fp_attempts,
    }
