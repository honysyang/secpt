"""水位线与断电续跑（WF8）：kill 后只复建不回放。

为什么单独一个模块：ledger 只管"怎么写 SQL"，本模块管"何时推进、续跑怎么
进场"——runs.step_count 是整段 WF8 的水位线，收尾时与 steps/小票在同一事务
里推进（:func:`step_transaction`），重启时按水位线重建进度（:func:`resume_load`）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from secpt.errors import LedgerError
from secpt.store import ledger
from secpt.store.ledger import RunRow, TaskRow


class ResumeState:
    """续跑进场包：目标投影（task/run 三问还原）+ 水位线快照。"""

    __slots__ = ("task", "run", "waterline")

    def __init__(self, task: TaskRow, run: RunRow) -> None:
        self.task = task
        self.run = run
        self.waterline = run.step_count

    def __repr__(self) -> str:
        return (
            f"ResumeState(task_id={self.task.id!r}, run_id={self.run.id!r},"
            f" waterline={self.waterline})"
        )


def mark_step(conn: sqlite3.Connection, run_id: str, step_no: int) -> None:
    """§6.2 API：水位线推进（只进不退、同号幂等，SQL 归 ledger）。"""
    ledger.mark_step(conn, run_id, step_no)


def resume_load(conn: sqlite3.Connection, task_id: str) -> ResumeState:
    """按 task_id 找最近 run：确认事故 → status 放回 running → 还原进场包。

    - 已正常收尾的 run 拒续（正常结束没有断电可言，误 resume 直接指错）；
    - waterline = run.step_count，其后残留（已被 kill 掉的半截步）忽略不回放。
    """
    run = ledger.latest_run(conn, task_id)
    if run.status == "ended":
        raise LedgerError(
            "最近一次 run 已正常收尾，无需续跑", context={"run_id": run.id, "task_id": task_id}
        )
    ledger.reopen_run(conn, run.id)
    return ResumeState(ledger.get_task(conn, task_id), ledger.get_run(conn, run.id))


@contextmanager
def step_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """WF8 收尾事务：steps + 小票 + 水位线同笔提交（事务边界委托 ledger.write_batch）。"""
    with ledger.write_batch(conn):
        yield


__all__ = ["ResumeState", "mark_step", "resume_load", "step_transaction"]
