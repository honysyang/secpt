"""租约（BUILD_SPEC §6.6）：过期回收。

resume 后仍在 running 的任务若租约已过期，转回 ready（transition 清租约、
落 task.state_changed{reason: lease_expired}，重放逐字段一致）。
"""
from __future__ import annotations

from datetime import timedelta

from pa.graph.state import transition
from pa.models import Lease, MissionState, now
from pa.store.writer import StoreWriter


def expire_leases(state: MissionState, writer: StoreWriter) -> int:
    """回收过期租约，返回回收数。v0 worker 同步执行，主要命中 resume 路径。"""
    n = 0
    for t in state.tasks.values():
        if t.state == "running" and t.lease and t.lease.expires < now():
            transition(t, "ready", writer, reason="lease_expired")
            n += 1
    return n


def make_lease(task, seconds_grace: int = 60) -> Lease:
    return Lease(owner=f"w-{task.id}",
                 expires=now() + timedelta(seconds=task.budget.seconds
                                           + seconds_grace))
