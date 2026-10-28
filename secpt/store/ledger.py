"""小票与账本的唯一合法写入口（§6.2：append-only 写，越权直写即 bug）。

为什么所有 INSERT 收敛到本文件：W1-1 已经用触发器把 append-only 机制化，本模块
是那条红线之上的"官门"——tasks/runs/steps/tickets/audit_log 的每一笔写入都从
这里过，事件刻点（ticket_appended）也在这里触发（§8：store 触发）。

行模型用 pydantic 强类型：sqlite Row → 模型只发生在一处转换，上层永远不是
"既写 SQL 又拼字典"。"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from secpt.errors import LedgerError

TicketObserver = Callable[[str, Mapping[str, object]], None]


def _now() -> int:
    """统一时间源：epoch 秒。"""
    return int(time.time())


class TaskRow(BaseModel):
    """tasks 行强类型视图。"""

    id: str
    input_text: str
    goal: str | None
    mode: str
    exit_kind: str | None
    created_at: int


class RunRow(BaseModel):
    """runs 行强类型视图：step_count 即水位线（W1-5 在同一事务里推进它）。"""

    id: str
    task_id: str
    status: str
    step_count: int
    config_json: str
    started_at: int
    ended_at: int | None


class StepRow(BaseModel):
    """steps 行强类型视图。"""

    id: str
    task_id: str
    run_id: str
    step_no: int
    plan_refs: str
    llm_output: str
    status: str
    started_at: int
    ended_at: int


class TicketRow(BaseModel):
    """tickets 行强类型视图（小票三字段：raw_ref/exit_code/summary）。"""

    id: str
    task_id: str
    run_id: str
    step_id: str
    tool_name: str
    args_json: str
    exit_code: int | None
    summary: str
    raw_ref: str
    created_at: int


def start_task(conn: sqlite3.Connection, input_text: str, goal: str) -> TaskRow:
    """WF1 落账第一行：tasks。有 goal 即 pentest 模式（对话模式不走本函数）。"""
    task = TaskRow(
        id=uuid.uuid4().hex,
        input_text=input_text,
        goal=goal,
        mode="pentest",
        exit_kind=None,
        created_at=_now(),
    )
    try:
        conn.execute(
            "INSERT INTO tasks (id, input_text, goal, mode, exit_kind, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task.id, task.input_text, task.goal, task.mode, task.exit_kind, task.created_at),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise LedgerError("tasks 写入失败", context={"input_text": input_text[:64]}) from exc
    return task


def start_run(
    conn: sqlite3.Connection, task_id: str, config: Mapping[str, Any] | None = None
) -> RunRow:
    """WF1 第二行：runs（status=running、step_count=0）。config 是本 run 参数快照。"""
    run = RunRow(
        id=uuid.uuid4().hex,
        task_id=task_id,
        status="running",
        step_count=0,
        config_json=json.dumps(dict(config or {}), ensure_ascii=False, sort_keys=True),
        started_at=_now(),
        ended_at=None,
    )
    try:
        conn.execute(
            "INSERT INTO runs (id, task_id, status, step_count, config_json, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run.id, run.task_id, run.status, run.step_count, run.config_json, run.started_at),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise LedgerError("runs 写入失败", context={"task_id": task_id}) from exc
    return run


def end_run(conn: sqlite3.Connection, run_id: str, *, status: str = "ended") -> None:
    """收尾写入：runs 是 UPDATE、合法（append-only 只锁 tickets/audit_log/findings）。"""
    try:
        conn.execute(
            "UPDATE runs SET status = ?, ended_at = ? WHERE id = ?", (status, _now(), run_id)
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise LedgerError("runs 收尾失败", context={"run_id": run_id}) from exc


def get_run(conn: sqlite3.Connection, run_id: str) -> RunRow:
    """按 id 读 run（resume/自检用）。查无此行抛 LedgerError——读不到就是账本错。"""
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise LedgerError("runs 查无此行", context={"run_id": run_id})
    return RunRow.model_validate(dict(row))


def recent_tickets(conn: sqlite3.Connection, task_id: str, *, limit: int = 10) -> list[TicketRow]:
    """喂注意力包的最近小票（新→旧截断）。纯读，无副作用。"""
    rows = conn.execute(
        "SELECT * FROM tickets WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [TicketRow.model_validate(dict(r)) for r in rows]


def record_step(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    step_no: int,
    llm_output: str,
    *,
    status: str = "ok",
) -> StepRow:
    """WF2 落账：steps 表挂本步结构化输出原文（审计盘根问底的锚）。"""
    step = StepRow(
        id=uuid.uuid4().hex,
        task_id=task_id,
        run_id=run_id,
        step_no=step_no,
        plan_refs="[]",
        llm_output=llm_output,
        status=status,
        started_at=_now(),
        ended_at=_now(),
    )
    try:
        conn.execute(
            "INSERT INTO steps (id, task_id, run_id, step_no, plan_refs, llm_output,"
            " status, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                step.id,
                step.task_id,
                step.run_id,
                step.step_no,
                step.plan_refs,
                step.llm_output,
                step.status,
                step.started_at,
                step.ended_at,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise LedgerError("steps 写入失败", context={"run_id": run_id, "step_no": step_no}) from exc
    return step


def append_ticket(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    step_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    exit_code: int | None,
    summary: str,
    raw_ref: str,
    bus: TicketObserver | None = None,
) -> TicketRow:
    """小票唯一写入口：INSERT + （有订阅者时）触发 ticket_appended。

    ``bus`` 用回调签名而非 import EventBus：store 不依赖 ob，谁建总线谁把回调
    塞进来（本应是 EventBus.emit 的 bind 形参，为解耦只取回调语义）。
    """
    ticket = TicketRow(
        id=uuid.uuid4().hex,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        tool_name=tool_name,
        args_json=json.dumps(dict(args), ensure_ascii=False, sort_keys=True),
        exit_code=exit_code,
        summary=summary,
        raw_ref=raw_ref,
        created_at=_now(),
    )
    try:
        conn.execute(
            "INSERT INTO tickets (id, task_id, run_id, step_id, tool_name, args_json,"
            " exit_code, summary, raw_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticket.id,
                ticket.task_id,
                ticket.run_id,
                ticket.step_id,
                ticket.tool_name,
                ticket.args_json,
                ticket.exit_code,
                ticket.summary,
                ticket.raw_ref,
                ticket.created_at,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise LedgerError("tickets 写入失败", context={"tool_name": tool_name}) from exc
    if bus is not None:
        bus(
            "ticket_appended",
            {
                "task_id": task_id,
                "run_id": run_id,
                "ticket_id": ticket.id,
                "tool_name": tool_name,
                "exit_code": exit_code,
                "summary": summary,
            },
        )
    return ticket


def audit(conn: sqlite3.Connection, task_id: str, actor: str, what: str) -> None:
    """audit_log 唯一写入口：每处显式调用，不得 omit（§5 重申为机制）。"""
    try:
        conn.execute(
            "INSERT INTO audit_log (task_id, actor, what, created_at) VALUES (?, ?, ?, ?)",
            (task_id, actor, what, _now()),
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise LedgerError(
            "audit_log 写入失败", context={"task_id": task_id, "actor": actor}
        ) from exc


__all__ = [
    "RunRow",
    "StepRow",
    "TaskRow",
    "TicketRow",
    "append_ticket",
    "audit",
    "end_run",
    "get_run",
    "recent_tickets",
    "record_step",
    "start_run",
    "start_task",
]
