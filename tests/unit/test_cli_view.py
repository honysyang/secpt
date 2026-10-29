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
    assert "【主LLM】第 3/? 步 · 开始决策" in out
    assert "【主LLM → nmap】exit=0 | 22/tcp open  ssh" in out
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
    assert "【主LLM】第 ?/? 步 · 开始决策" in out
    assert "【主LLM → ?】" in out
    assert "【出口】exit_kind=?" in out


def test_cli_view_renders_worker_events_and_durations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M5-29：worker 每步/每票上屏，主链与 worker 的耗时都人类化。"""
    bus = EventBus()
    bus.subscribe(CliView())
    bus.emit("run_step_started", step_no=1, max_steps=10)
    bus.emit(
        "ticket_appended",
        tool_name="nmap",
        exit_code=0,
        summary="22/tcp open",
        duration_ms=1200,
    )
    bus.emit(
        "graph_changed",
        step_no=1,
        thought="扫一下",
        tools="nmap",
        nodes=1,
        edges=0,
        duration_ms=3400,
    )
    bus.emit("worker_step_started", actor="workerA", step_no=2, step_budget=8)
    bus.emit(
        "worker_ticket",
        actor="workerA",
        tool_name="http_get",
        exit_code=0,
        summary="HTTP 200",
        duration_ms=900,
    )
    bus.emit(
        "worker_step_ended",
        actor="workerA",
        step_no=2,
        duration_ms=1500,
        thought="拿到证书日志",
    )
    bus.emit(
        "worker_mission_ended",
        actor="workerA",
        finished=True,
        steps=2,
        findings=1,
        duration_ms=12300,
    )
    bus.emit(
        "run_ended",
        exit_kind="normal",
        steps=1,
        findings=1,
        duration_ms=125000,
        task_id="t1",
    )
    out = capsys.readouterr().out
    assert "第 1/10 步 · 开始决策" in out
    assert "【主LLM】第 1 步决策 · 3.4s：扫一下（工具：nmap）" in out
    assert "【主LLM → nmap】exit=0（1.2s） | 22/tcp open" in out
    assert "【workerA】第 2/8 步 · 开始" in out
    assert "【workerA → http_get】exit=0（0.9s） | HTTP 200" in out
    assert "【workerA】第 2 步完成（1.5s）：拿到证书日志" in out
    assert "【workerA·收单】完工 steps=2 findings=1 用时=12.3s" in out
    assert "【出口】exit_kind=normal steps=1 findings=1 用时=2m05s" in out


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


def test_cli_view_renders_step_decision_and_exit_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """M5-26：每步要讲"想了什么/用了哪个工具"；出场未过要给失败明细。"""
    view = CliView()
    view(
        "graph_changed", {"step_no": 2, "thought": "扫 22", "tools": "nmap", "nodes": 1, "edges": 0}
    )
    view("exit_failed", {"streak": 1, "failures": ["claimed 悬置 1 条未收口"]})
    out = capsys.readouterr().out
    assert "【主LLM】第 2 步决策：扫 22（工具：nmap）" in out
    assert "【双图】+1n+0e" in out
    assert "【出场】第 1 轮未过：claimed 悬置 1 条未收口" in out


# ---- M5-34 审计窗翻新：findings 状态机明细 / observations / 去掉陈旧文案 ----------


def test_evidence_report_shows_finding_states_and_observations() -> None:
    """审计窗要能判断问题：每条 finding 的判态/方法/锚点 + observations 客观流。"""
    from secpt.store import ledger, observations
    from secpt.store import migrate as migrate_store

    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "审计演示", goal="10.0.0.5")
    ledger.append_finding(conn, task.id, "22 开放", "banner 在档", "raw/a.txt")
    conn.execute("UPDATE findings SET verify_state='verified', method='oracle'")
    conn.commit()
    observations.append(conn, task.id, "r1", "http", {"mock": True}, "raw/obs_1.txt")

    lines = evidence_report(conn, task.id)
    blob = "\n".join(lines)

    assert "-- findings --" in blob
    assert "[verified|oracle|sweep=0] main: 22 开放 → raw/a.txt" in blob
    assert "detail: banner 在档" in blob
    assert "M1 无裁判" not in blob  # 陈旧文案退场
    assert "-- observations -- 1 条" in blob
    conn.close()
