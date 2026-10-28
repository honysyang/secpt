"""W1-7 contract test：CliView 角色气泡/小票摘要行 + evidence 只读检查窗。"""

from __future__ import annotations

import sqlite3

import pytest

from secpt.errors import LedgerError
from secpt.ob.cli_view import CliView, evidence_report
from secpt.ob.events import EventBus
from secpt.store import ledger
from secpt.store.migrate import connect

# ---- CliView：EventBus 订阅者（对话面板渲染） --------------------------------


def test_cli_view_renders_three_m1_events(capsys: pytest.CaptureFixture[str]) -> None:
    bus = EventBus()
    bus.subscribe(CliView())
    bus.emit("run_step_started", task_id="t1", run_id="r1", step_no=3)
    bus.emit(
        "ticket_appended",
        task_id="t1",
        run_id="r1",
        ticket_id="k1",
        tool_name="nmap",
        exit_code=0,
        summary="22/tcp open  ssh",
    )
    bus.emit("run_ended", task_id="t1", run_id="r1", steps=2, finished=True, exit_kind="normal")
    out = capsys.readouterr().out
    assert "【Orchestrator】step 3 开包" in out
    assert "【小票】nmap | exit=0 | 22/tcp open  ssh" in out
    assert "【出口】exit_kind=normal" in out
    assert "steps=2" in out


def test_cli_view_tolerates_missing_payload_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """载荷缺键不炸：可观测是旁观者，绝不是主循环撒手不管就能崩的地方。"""
    view = CliView()
    view("run_step_started", {})
    view("ticket_appended", {})
    view("run_ended", {})
    out = capsys.readouterr().out
    assert "【Orchestrator】step ?" in out
    assert "【小票】?" in out
    assert "【出口】exit_kind=?" in out


def test_cli_view_ignores_unknown_events(capsys: pytest.CaptureFixture[str]) -> None:
    """M1 契约只有三事件；M2 的新事件名不渲染，等接线再上屏。"""
    CliView()("finding_verdict", {"id": "f1"})
    assert capsys.readouterr().out == ""


# ---- evidence：账本落地检查窗（只读 SELECT） -----------------------------------


def _seed_task(conn: sqlite3.Connection) -> str:
    """最小可读账本：进场 + 一步 + 一张 nmap 小票 + 一条审计。"""
    task = ledger.start_task(conn, input_text="内网摸底", goal="10.0.0.1")
    run = ledger.start_run(conn, task.id, config={"objective": "测式", "success_criteria": "稳"})
    step = ledger.record_step(conn, task.id, run.id, 1, "{}")
    ledger.append_ticket(
        conn,
        task.id,
        run.id,
        step.id,
        "nmap",
        {"target": "10.0.0.1"},
        0,
        "22/tcp open  ssh",
        "raw/nmap_demo.txt",
        bus=None,
    )
    ledger.audit(conn, task.id, "main", "进场三问完成")
    return task.id


def test_evidence_report_covers_ledger_tables() -> None:
    conn = connect(":memory:")
    try:
        task_id = _seed_task(conn)
        blob = "\n".join(evidence_report(conn, task_id))
    finally:
        conn.close()
    assert "== evidence · task" in blob
    assert "goal=10.0.0.1" in blob
    assert "runs" in blob and "step_count" in blob
    assert "nmap | exit=0 | 22/tcp open  ssh" in blob
    assert "raw/nmap_demo.txt" in blob  # 锚点必须可核
    assert "findings" in blob
    assert "main: 进场三问完成" in blob


def test_evidence_report_shows_run_waterline() -> None:
    conn = connect(":memory:")
    try:
        task_id = _seed_task(conn)
        run = ledger.latest_run(conn, task_id)
        ledger.mark_step(conn, run.id, 1)
        ledger.end_run(conn, run.id)
        blob = "\n".join(evidence_report(conn, task_id))
    finally:
        conn.close()
    assert "ended" in blob and "step_count=1" in blob


def test_evidence_unknown_task_raises() -> None:
    conn = connect(":memory:")
    with pytest.raises(LedgerError):
        # 查无此行必须显式炸（LedgerError），main.py 兜底转非零出口
        evidence_report(conn, "no-such-task")
