"""ledger 契约：官门写入 + append-only 触发器失败注入。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

import pytest

from secpt.errors import LedgerError
from secpt.store import ledger, migrate


@pytest.fixture()
def conn() -> Callable[[], sqlite3.Connection]:
    def _make() -> sqlite3.Connection:
        return migrate.connect(":memory:")

    return _make


def test_start_task_and_run_persist_rows(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task = ledger.start_task(c, input_text="摸清 192.168.1.0/24 的服务", goal="摸底")
    run = ledger.start_run(c, task.id, config={"objective": "扫出局"})
    fetched = ledger.get_run(c, run.id)
    assert fetched.step_count == 0
    assert fetched.status == "running"
    assert "objective" in fetched.config_json
    row = c.execute("SELECT * FROM tasks WHERE id = ?", (task.id,)).fetchone()
    assert row["mode"] == "pentest"
    assert row["input_text"] == "摸清 192.168.1.0/24 的服务"


def test_start_run_fk_violation_raises_ledger_error(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    with pytest.raises(LedgerError):
        ledger.start_run(c, "no-such-task")


def test_end_run_marks_status(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task = ledger.start_task(c, "in", "goal")
    run = ledger.start_run(c, task.id)
    ledger.end_run(c, run.id)
    fetched = ledger.get_run(c, run.id)
    assert fetched.status == "ended"
    assert fetched.ended_at is not None


def test_append_ticket_triggers_observer(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task = ledger.start_task(c, "in", "goal")
    run = ledger.start_run(c, task.id)
    step = ledger.record_step(c, task.id, run.id, 1, "{}")
    events: list[tuple[str, Mapping[str, object]]] = []

    def observer(name: str, payload: Mapping[str, object]) -> None:
        events.append((name, payload))

    ticket = ledger.append_ticket(
        c,
        task.id,
        run.id,
        step.id,
        "nmap",
        {"ports": "80"},
        0,
        "nmap 扫描完成",
        "runs/abc/stdout.txt",
        bus=observer,
    )
    assert len(events) == 1
    name, payload = events[0]
    assert name == "ticket_appended"
    assert payload["ticket_id"] == ticket.id
    assert payload["tool_name"] == "nmap"
    assert payload["exit_code"] == 0
    row = c.execute("SELECT * FROM tickets WHERE id = ?", (ticket.id,)).fetchone()
    assert row["args_json"] == '{"ports": "80"}'
    assert row["raw_ref"] == "runs/abc/stdout.txt"


def test_recent_tickets_orders_desc(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task = ledger.start_task(c, "in", "goal")
    run = ledger.start_run(c, task.id)
    step = ledger.record_step(c, task.id, run.id, 1, "{}")
    for seq in range(3):
        ledger.append_ticket(c, task.id, run.id, step.id, f"t{seq}", {}, None, f"第{seq}张", "r")
    recent = ledger.recent_tickets(c, task.id, limit=2)
    assert len(recent) == 2
    assert [t.summary for t in recent] == ["第2张", "第1张"]


def test_get_run_missing_raises_ledger_error(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    with pytest.raises(LedgerError, match="查无此行"):
        ledger.get_run(c, "missing")


def test_audit_log_write(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task = ledger.start_task(c, "in", "goal")
    ledger.audit(c, task.id, "main", "进场三问完成")
    row = c.execute("SELECT * FROM audit_log WHERE task_id = ?", (task.id,)).fetchone()
    assert row["actor"] == "main"
    assert row["what"] == "进场三问完成"


def test_invalid_mode_rejected_by_check(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """schema 级防线：mode 越界值直接被 CHECK 拒掉。"""
    c = conn()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO tasks (id, input_text, mode, created_at)"
            " VALUES ('bad', 'x', 'not-a-mode', 0)"
        )


def test_append_only_triggers_block_overwrite(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """失败注入：小票/审计写完以后任何人想 UPDATE/DELETE 都必须被触发器当场拒掉。"""
    c = conn()
    task = ledger.start_task(c, "in", "goal")
    run = ledger.start_run(c, task.id)
    step = ledger.record_step(c, task.id, run.id, 1, "{}")
    ledger.append_ticket(c, task.id, run.id, step.id, "nmap", {}, 0, "原句", "r")
    with pytest.raises(sqlite3.IntegrityError, match="E_APPEND_ONLY"):
        c.execute("UPDATE tickets SET summary = '篡改'")
        c.commit()


def test_append_only_blocks_audit_and_findings_delete(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task = ledger.start_task(c, "in", "goal")
    ledger.audit(c, task.id, "main", "进场")
    with pytest.raises(sqlite3.IntegrityError, match="E_APPEND_ONLY"):
        c.execute("DELETE FROM audit_log")
        c.commit()


# ---- W2-1 findings 状态机骨架（claimed → verified/rejected）--------------------


def _seed(c: sqlite3.Connection) -> str:
    return ledger.start_task(c, "in", "goal").id


def test_append_finding_defaults_claimed(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed(c)
    finding = ledger.append_finding(c, task_id, "端口 22 开放", "ssh banner 已取", "runs/a.txt")
    assert finding.verify_state == "claimed"
    assert finding.method is None and finding.judged_at is None
    fetched = ledger.task_findings(c, task_id)
    assert [f.id for f in fetched] == [finding.id]


def test_append_finding_rejects_illegal_state_and_method(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed(c)
    with pytest.raises(LedgerError, match="verify_state"):
        ledger.append_finding(c, task_id, "t", "d", "r", verify_state="foo")
    with pytest.raises(LedgerError, match="method"):
        ledger.append_finding(c, task_id, "t", "d", "r", method="拍脑袋")


def test_set_finding_state_claimed_transitions(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """两条合法迁移：claimed→verified（method 记档）/ claimed→rejected。"""
    c = conn()
    task_id = _seed(c)
    a = ledger.append_finding(c, task_id, "甲", "d", "r")
    verified = ledger.set_finding_state(c, a.id, "verified", method="manual")
    assert verified.verify_state == "verified"
    assert verified.method == "manual"
    assert verified.judged_at is not None
    b = ledger.append_finding(c, task_id, "乙", "d", "r")
    rejected = ledger.set_finding_state(c, b.id, "rejected")
    assert rejected.verify_state == "rejected"


def test_set_finding_state_terminal_is_locked(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed(c)
    finding = ledger.append_finding(c, task_id, "甲", "d", "r", verify_state="verified")
    with pytest.raises(LedgerError, match="终态"):
        ledger.set_finding_state(c, finding.id, "rejected")


def test_set_finding_state_rejects_illegal_target_state(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed(c)
    finding = ledger.append_finding(c, task_id, "甲", "d", "r")
    with pytest.raises(LedgerError, match="非法"):
        ledger.set_finding_state(c, finding.id, "claimed")


def test_set_finding_state_missing_row_raises(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    with pytest.raises(LedgerError, match="查无此行"):
        ledger.set_finding_state(c, "no-such", "verified")


def test_append_only_blocks_findings_update_to_illegal_state(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """失败注入：绕过官门直写 findings 也被 schema CHECK 当场拒绝。"""
    c = conn()
    task_id = _seed(c)
    ledger.append_finding(c, task_id, "甲", "d", "r")
    with pytest.raises(sqlite3.IntegrityError, match="verify_state IN"):
        c.execute("UPDATE findings SET verify_state = 'foo'")
