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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

from secpt.errors import LedgerError

TicketObserver = Callable[[str, Mapping[str, object]], None]

# 批量事务记账表：sqlite3.Connection 既不能挂属性（无 __dict__）也不能弱引用，
# 批量状态只能记在模块级；id → 强引用（存活的连接 id 不会被复用，防误判），
# finally 保证清表不泄漏。
_BATCHED: dict[int, sqlite3.Connection] = {}


def _now() -> int:
    """统一时间源：epoch 秒。"""
    return int(time.time())


def _maybe_commit(conn: sqlite3.Connection) -> None:
    """写函数统一收口：不在批量内当场 commit；在批量内服从外层边界。

    单笔写（历史路径）保持"写一笔交一笔"的旧语义；批量收尾（WF8）由
    :func:`write_batch` 一锤定音——批内任何写函数都没有越权递交的权力。
    """
    if id(conn) not in _BATCHED:
        conn.commit()


@contextmanager
def write_batch(conn: sqlite3.Connection) -> Iterator[None]:
    """多笔写入批成一个事务（WF8 收尾原子性的机制底座）。

    - 进场要求"干净交易"：批嵌套、或带着未提交脏状态进场一律 LedgerError，
      宁可拒绝也不悄悄吞掉别人的未提交作业；
    - 批内所有写函数经 :func:`_maybe_commit` 放弃私下 commit；
    - 出场一锤定音：正常 → COMMIT；任何异常 → 整批 ROLLBACK 后原样上抛。
    """
    if id(conn) in _BATCHED:
        raise LedgerError("write_batch 不允许嵌套", context={"conn_id": id(conn)})
    if conn.in_transaction:
        raise LedgerError("write_batch 需要干净的账本连接（存在未提交脏状态）")
    conn.execute("BEGIN IMMEDIATE")
    _BATCHED[id(conn)] = conn
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        _BATCHED.pop(id(conn), None)


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


class FindingRow(BaseModel):
    """findings 行强类型视图：状态机 claimed→verified/rejected（verify 管真伪的骨架）。"""

    id: str
    task_id: str
    title: str
    detail: str
    verify_state: str
    method: str | None
    evidence_ref: str
    confidence: float | None
    judged_at: int | None
    created_at: int


_VERIFY_STATES = frozenset({"claimed", "verified", "rejected"})
_VERIFY_METHODS = frozenset({"rerun", "dual-path", "negative", "oracle", "manual"})
_TERMINAL_STATES = frozenset({"verified", "rejected"})


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
        _maybe_commit(conn)
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
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("runs 写入失败", context={"task_id": task_id}) from exc
    return run


def end_run(conn: sqlite3.Connection, run_id: str, *, status: str = "ended") -> None:
    """收尾写入：runs 是 UPDATE、合法（append-only 只锁 tickets/audit_log/findings）。"""
    try:
        conn.execute(
            "UPDATE runs SET status = ?, ended_at = ? WHERE id = ?", (status, _now(), run_id)
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("runs 收尾失败", context={"run_id": run_id}) from exc


def get_run(conn: sqlite3.Connection, run_id: str) -> RunRow:
    """按 id 读 run（resume/自检用）。查无此行抛 LedgerError——读不到就是账本错。"""
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise LedgerError("runs 查无此行", context={"run_id": run_id})
    return RunRow.model_validate(dict(row))


def latest_run(conn: sqlite3.Connection, task_id: str) -> RunRow:
    """该 task 最近一次 run（WF8 续跑的入口查询）：started_at 降序、同秒用 rowid。"""
    row = conn.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        raise LedgerError("该 task 查无 run", context={"task_id": task_id})
    return RunRow.model_validate(dict(row))


def mark_step(conn: sqlite3.Connection, run_id: str, step_no: int) -> None:
    """水位线唯一推进口：只进不退、同号幂等、干净失败（不落半截脏事务）。

    为什么 SQL 收在 ledger 而语义在 checkpointer：写入口只经 store/*（铁律），
    核心的"何时推进"由 core.checkpointer.mark_step 委托到这里。
    """
    row = conn.execute("SELECT step_count FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise LedgerError("水位线推进失败：runs 查无此行", context={"run_id": run_id})
    current = int(row["step_count"])
    if step_no < current:
        raise LedgerError(
            "水位线倒退被拒（只进不退）",
            context={"run_id": run_id, "current": current, "target": step_no},
        )
    if step_no == current:
        return  # 同号重放 = 幂等 no-op（纯 SELECT 不开预备事务，零副作用返回）
    try:
        conn.execute("UPDATE runs SET step_count = ? WHERE id = ?", (step_no, run_id))
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("水位线推进失败", context={"run_id": run_id, "step_no": step_no}) from exc


def reopen_run(conn: sqlite3.Connection, run_id: str) -> None:
    """事故复建：被 kill 的 run 放回 running（已正常收尾的 run 拒绝复活）。"""
    if conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
        raise LedgerError("run 复建失败：runs 查无此行", context={"run_id": run_id})
    try:
        conn.execute(
            "UPDATE runs SET status = 'running', ended_at = NULL"
            " WHERE id = ? AND status != 'ended'",
            (run_id,),
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("run 复建失败", context={"run_id": run_id}) from exc


def end_task(conn: sqlite3.Connection, task_id: str, exit_kind: str) -> None:
    """tasks.exit_kind 收尾写入（WF7：normal/abnormal/stalled 三值）。

    为什么在 tasks 而非 runs：异常出口的"为什么结束"是 task 级事实（LLMA 半场
    报告、CLAU 终场都按 task 交差），run 只是本次执行载体。非法 exit_kind 在
    本函数拒绝——schema 的 CHECK 是最后一道闸，这里是第一道。
    """
    if exit_kind not in {"normal", "abnormal", "stalled"}:
        raise LedgerError("exit_kind 非法", context={"task_id": task_id, "exit_kind": exit_kind})
    try:
        conn.execute("UPDATE tasks SET exit_kind = ? WHERE id = ?", (exit_kind, task_id))
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("tasks 收尾失败", context={"task_id": task_id}) from exc


def get_task(conn: sqlite3.Connection, task_id: str) -> TaskRow:
    """按 id 读 task（resume 还原目标投影用）。查无此行抛 LedgerError。"""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise LedgerError("tasks 查无此行", context={"task_id": task_id})
    return TaskRow.model_validate(dict(row))


def latest_task(conn: sqlite3.Connection) -> TaskRow:
    """最近一个 task（main.py 半场报告兜底取材用）：created_at 降序、同秒用 rowid。"""
    row = conn.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LedgerError("tasks 查无此行（库为空）", context={})
    return TaskRow.model_validate(dict(row))


def ticket_event_payload(ticket: TicketRow) -> dict[str, object]:
    """ticket_appended 事件载荷的唯一构造点（append_ticket 与 loop 批后补发共用）。"""
    return {
        "task_id": ticket.task_id,
        "run_id": ticket.run_id,
        "ticket_id": ticket.id,
        "tool_name": ticket.tool_name,
        "exit_code": ticket.exit_code,
        "summary": ticket.summary,
    }


def recent_tickets(conn: sqlite3.Connection, task_id: str, *, limit: int = 10) -> list[TicketRow]:
    """喂注意力包的最近小票（新→旧截断）。纯读，无副作用。"""
    rows = conn.execute(
        # 同秒多张小票时 created_at 平局：id 是随机 uuid 不能当序，用隐式 rowid
        # （插入序，append-only 表保证单调）做 tie-break，保证"最近"确定。
        "SELECT * FROM tickets WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
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
    plan_refs: Sequence[str] = (),
) -> StepRow:
    """WF2 落账：steps 表挂本步结构化输出原文（审计盘根问底的锚）。

    ``plan_refs``（M5）：本步新增规划行的 id 清单——规划与小票/水位线同事务
    落账，事后能复算"哪一步提出了哪条规划"。
    """
    step = StepRow(
        id=uuid.uuid4().hex,
        task_id=task_id,
        run_id=run_id,
        step_no=step_no,
        plan_refs=json.dumps(list(plan_refs), ensure_ascii=False),
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
        _maybe_commit(conn)
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
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("tickets 写入失败", context={"tool_name": tool_name}) from exc
    if bus is not None:
        bus("ticket_appended", ticket_event_payload(ticket))
    return ticket


def audit(conn: sqlite3.Connection, task_id: str, actor: str, what: str) -> None:
    """audit_log 唯一写入口：每处显式调用，不得 omit（§5 重申为机制）。"""
    try:
        conn.execute(
            "INSERT INTO audit_log (task_id, actor, what, created_at) VALUES (?, ?, ?, ?)",
            (task_id, actor, what, _now()),
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError(
            "audit_log 写入失败", context={"task_id": task_id, "actor": actor}
        ) from exc


def append_finding(
    conn: sqlite3.Connection,
    task_id: str,
    title: str,
    detail: str,
    evidence_ref: str,
    *,
    verify_state: str = "claimed",
    method: str | None = None,
    confidence: float | None = None,
) -> FindingRow:
    """findings 唯一写入口（新增侧）：M1 供测试/预置与 M2 裁判接线用。

    verify_state/method 先在应用层校验（本校验消息可读），schema CHECK 兜底——
    越过官门直写的人只会从触发器/CHECK 拿到裸 IntegrityError。
    """
    if verify_state not in _VERIFY_STATES:
        raise LedgerError(
            "非法 verify_state", context={"task_id": task_id, "verify_state": verify_state}
        )
    if method is not None and method not in _VERIFY_METHODS:
        raise LedgerError("非法 method", context={"task_id": task_id, "method": method})
    finding = FindingRow(
        id=uuid.uuid4().hex,
        task_id=task_id,
        title=title,
        detail=detail,
        verify_state=verify_state,
        method=method,
        evidence_ref=evidence_ref,
        confidence=confidence,
        judged_at=_now() if verify_state != "claimed" else None,
        created_at=_now(),
    )
    try:
        conn.execute(
            "INSERT INTO findings (id, task_id, title, detail, verify_state, method,"
            " evidence_ref, confidence, judged_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding.id,
                finding.task_id,
                finding.title,
                finding.detail,
                finding.verify_state,
                finding.method,
                finding.evidence_ref,
                finding.confidence,
                finding.judged_at,
                finding.created_at,
            ),
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("findings 写入失败", context={"task_id": task_id}) from exc
    return finding


def task_findings(
    conn: sqlite3.Connection, task_id: str, *, verify_state: str | None = None
) -> list[FindingRow]:
    """按 task 读 findings（可按状态过滤，reporter 的 verified-only 从这里取）。纯读。"""
    if verify_state is not None and verify_state not in _VERIFY_STATES:
        raise LedgerError(
            "非法 verify_state", context={"task_id": task_id, "verify_state": verify_state}
        )
    sql = "SELECT * FROM findings WHERE task_id = ?"
    params: list[object] = [task_id]
    if verify_state is not None:
        sql += " AND verify_state = ?"
        params.append(verify_state)
    sql += " ORDER BY created_at, rowid"
    rows = conn.execute(sql, params).fetchall()
    return [FindingRow.model_validate(dict(r)) for r in rows]


def set_finding_state(
    conn: sqlite3.Connection, finding_id: str, new_state: str, *, method: str | None = None
) -> FindingRow:
    """findings 状态机唯一推进口：verified/rejected 是终态，向后一律拒绝。

    为什么锁终态：verify 管真伪的骨架允许"补证"但禁止"翻案"——M1 不实现裁判，
    审计上任何改判都应该是一次新 finding，而不是悄悄改老行。
    """
    row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise LedgerError("findings 查无此行", context={"finding_id": finding_id})
    current = FindingRow.model_validate(dict(row))
    if current.verify_state in _TERMINAL_STATES:
        raise LedgerError(
            "finding 已是终态，禁止改判",
            context={"finding_id": finding_id, "current": current.verify_state},
        )
    if new_state not in {"verified", "rejected"}:
        raise LedgerError("非法状态迁移", context={"finding_id": finding_id, "target": new_state})
    if method is not None and method not in _VERIFY_METHODS:
        raise LedgerError("非法 method", context={"finding_id": finding_id, "method": method})
    try:
        conn.execute(
            "UPDATE findings SET verify_state = ?, method = COALESCE(?, method),"
            " judged_at = ? WHERE id = ?",
            (new_state, method, _now(), finding_id),
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("findings 推进失败", context={"finding_id": finding_id}) from exc
    return get_finding(conn, finding_id)


def get_finding(conn: sqlite3.Connection, finding_id: str) -> FindingRow:
    """按 id 读 finding。查无此行抛 LedgerError。"""
    row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise LedgerError("findings 查无此行", context={"finding_id": finding_id})
    return FindingRow.model_validate(dict(row))


__all__ = [
    "FindingRow",
    "RunRow",
    "StepRow",
    "TaskRow",
    "TicketRow",
    "append_finding",
    "append_ticket",
    "audit",
    "end_run",
    "end_task",
    "get_finding",
    "get_run",
    "get_task",
    "latest_run",
    "latest_task",
    "mark_step",
    "recent_tickets",
    "record_step",
    "reopen_run",
    "set_finding_state",
    "start_run",
    "start_task",
    "task_findings",
    "ticket_event_payload",
    "write_batch",
]
