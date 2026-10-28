"""W2-1 contract：出场三验 + LLMA 终场/半场报告（verified-only + 锚点硬规）。

全部离线：FakeLLM 脚本化喂报告摘要，账本用 :memory: 预置——
claims 只认账本锚点，LLM 结构上进不了 findings 列表（单测主战场）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest

from secpt.core.loop import ExitVerdict
from secpt.errors import LedgerError, LlmError
from secpt.finalize import exit_checklist, reporter
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.store import ledger, migrate


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    c = migrate.connect(":memory:")
    yield c
    c.close()


def _task_with_run(
    conn: sqlite3.Connection, *, objective: str = "o", criteria: str = "s"
) -> ledger.TaskRow:
    task = ledger.start_task(conn, "input", goal="10.0.0.9")
    ledger.start_run(conn, task.id, config={"objective": objective, "success_criteria": criteria})
    return task


def _summary_script(text: str) -> LlmClient:
    return LlmClient(FakeLLM([json.dumps({"summary": text}, ensure_ascii=False)]))


# ---- 出场三验（WF6）============================================================


def test_checklist_passes_on_clean_ledger(conn: sqlite3.Connection) -> None:
    task = _task_with_run(conn)
    verdict = exit_checklist.run_checklist(conn, task.id)
    assert verdict.passed is True
    assert verdict.failures == []


def test_checklist_fails_on_orphan_ticket(conn: sqlite3.Connection) -> None:
    """账目缺口：有图节点（小票）无对应 step = 账本交不齐。"""
    task = _task_with_run(conn)
    run = ledger.latest_run(conn, task.id)
    ledger.append_ticket(conn, task.id, run.id, "ghost-step", "nmap", {}, 0, "孤票", "r")
    verdict = exit_checklist.run_checklist(conn, task.id)
    assert verdict.passed is False
    assert any("孤儿" in f for f in verdict.failures)


def test_checklist_fails_on_pending_claim(conn: sqlite3.Connection) -> None:
    task = _task_with_run(conn)
    ledger.append_finding(conn, task.id, "悬置发现", "d", "r")
    verdict = exit_checklist.run_checklist(conn, task.id)
    assert verdict.passed is False
    assert any("claimed" in f for f in verdict.failures)


def test_checklist_fails_on_missing_questions(conn: sqlite3.Connection) -> None:
    """进场三问从账本还原，缺项即不过（criteria 原文如实出现在 detail）。"""
    task = ledger.start_task(conn, "input", goal="10.0.0.9")
    ledger.start_run(conn, task.id, config={})
    verdict = exit_checklist.run_checklist(conn, task.id)
    assert verdict.passed is False
    assert any("三问" in f for f in verdict.failures)


def test_exit_checklist_implements_core_protocol(conn: sqlite3.Connection) -> None:
    task = _task_with_run(conn)
    verdict = exit_checklist.ExitChecklist().check(conn, task.id)
    assert isinstance(verdict, ExitVerdict)


# ---- LLMA 报告（WF9）===========================================================


def test_report_verified_only_with_anchor(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    task = _task_with_run(conn)
    run = ledger.latest_run(conn, task.id)
    step = ledger.record_step(conn, task.id, run.id, 1, "{}")
    ledger.append_finding(conn, task.id, "悬置发现", "d", "r")  # claimed 永不进报告
    anchor = f"raw/{step.id}/stdout.txt"
    ledger.append_finding(
        conn, task.id, "开放端口 22", "ssh 起在 22", anchor, verify_state="verified"
    )
    ledger.end_task(conn, task.id, "normal")
    rep = reporter.Reporter(_summary_script("确认 22 开放")).generate(conn, task.id)
    assert [f.title for f in rep.findings] == ["开放端口 22"]
    assert rep.findings[0].evidence_ref == anchor
    assert rep.summary == "确认 22 开放"
    assert rep.half_court is False and rep.exit_kind == "normal"
    assert rep.failing_counts == {"exit_checklist": 0, "abnormal": 0}
    md = reporter.render_markdown(rep)
    assert anchor in md and "开放端口 22" in md
    assert "终场" in md
    capsys.readouterr()  # 静默 consuming：本套件只断言返回值


def test_report_drops_blank_anchor_and_audits(conn: sqlite3.Connection) -> None:
    """锚点校验是机制硬规：evidence_ref 空白的 verified 行剔除 + audit 留痕。"""
    task = _task_with_run(conn)
    ledger.append_finding(conn, task.id, "无锚行", "d", "  ", verify_state="verified")
    rep = reporter.Reporter(_summary_script("无可锚发现")).generate(conn, task.id)
    assert rep.findings == []
    audits = conn.execute(
        "SELECT what FROM audit_log WHERE actor = 'llma' AND what LIKE '%剔除%'"
    ).fetchall()
    assert len(audits) == 1


def test_report_half_court_flag(conn: sqlite3.Connection) -> None:
    task = _task_with_run(conn)
    rep = reporter.Reporter(_summary_script("s")).generate(conn, task.id, half_court=True)
    assert rep.half_court is True
    assert "半场" in reporter.render_markdown(rep)


def test_report_counts_abnormal_and_checklist_failures(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    task = _task_with_run(conn)
    ledger.append_finding(conn, task.id, "悬置", "d", "r")  # 三验缺口 +1
    ledger.end_task(conn, task.id, "abnormal")
    rep = reporter.Reporter(_summary_script("s")).generate(conn, task.id)
    assert rep.failing_counts == {"exit_checklist": 1, "abnormal": 1}
    assert rep.exit_kind == "abnormal"
    capsys.readouterr()


def test_report_llm_error_propagates(conn: sqlite3.Connection) -> None:
    """库层不吞错：FakeLLM 脚本耗尽等 LlmError 上抛，main.py 兜底降级。"""
    task = _task_with_run(conn)
    with pytest.raises(LlmError):
        reporter.Reporter(LlmClient(FakeLLM([]))).generate(conn, task.id)


def test_report_unknown_task_raises_ledger_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(LedgerError, match="查无此行"):
        reporter.Reporter(_summary_script("s")).generate(conn, "no-such-task")
