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
from secpt.store import ledger, migrate, plan


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


# ---- 出场清单⑥ 零进展步（M5-27）------------------------------------------------


def test_checklist_fails_on_zero_progress_step(conn: sqlite3.Connection) -> None:
    """有回执但全失败 = 零进展步：status 记 ok 也不许正常出场。"""
    task = _task_with_run(conn)
    run = ledger.latest_run(conn, task.id)
    step = ledger.record_step(conn, task.id, run.id, 1, "{}")
    ledger.append_ticket(
        conn, task.id, run.id, step.id, "nmap", {}, None, "E_SCHEMA_TOOL: 未知参数", ""
    )
    verdict = exit_checklist.run_checklist(conn, task.id)
    assert verdict.passed is False
    assert any("收工步零进展" in f and "step 1" in f for f in verdict.failures)


def test_checklist_ignores_earlier_failed_step(conn: sqlite3.Connection) -> None:
    """历史失败步不追罚：只判收工步——否则一次 typo 会永久封印正常出场。"""
    task = _task_with_run(conn)
    run = ledger.latest_run(conn, task.id)
    bad = ledger.record_step(conn, task.id, run.id, 1, "{}")
    ledger.append_ticket(conn, task.id, run.id, bad.id, "nmap", {}, None, "E_SCHEMA_TOOL", "")
    ledger.record_step(conn, task.id, run.id, 2, "{}")  # 收工步：无工具调用
    assert exit_checklist.run_checklist(conn, task.id).passed is True


def test_checklist_ignores_denied_only_step(conn: sqlite3.Connection) -> None:
    """denied 步与失速计同口径：闸门没放行 ≠ 零进展（病因在授权不在打法）。"""
    task = _task_with_run(conn)
    run = ledger.latest_run(conn, task.id)
    step = ledger.record_step(conn, task.id, run.id, 1, "{}")
    ledger.append_ticket(
        conn, task.id, run.id, step.id, "nmap", {}, None, "E_GUARD_DENY: 不在白名单", ""
    )
    assert exit_checklist.run_checklist(conn, task.id).passed is True


def test_checklist_passes_when_step_has_one_success(conn: sqlite3.Connection) -> None:
    """边界：同一步里有一张成功的回执就算有进展（失败小票不抹掉真实产出）。"""
    task = _task_with_run(conn)
    run = ledger.latest_run(conn, task.id)
    step = ledger.record_step(conn, task.id, run.id, 1, "{}")
    ledger.append_ticket(conn, task.id, run.id, step.id, "nmap", {}, None, "E_SCHEMA_TOOL", "")
    ledger.append_ticket(conn, task.id, run.id, step.id, "nmap", {}, 0, "22/tcp open", "raw/a")
    assert exit_checklist.run_checklist(conn, task.id).passed is True


# ---- 出场清单③ 交付对目标（M5）------------------------------------------------


def _acceptance(*pairs: tuple[str, str]) -> list[exit_checklist.AcceptanceRule]:
    return [exit_checklist.AcceptanceRule(id=key, pattern=pattern) for key, pattern in pairs]


def test_acceptance_passes_when_verified_finding_matches(conn: sqlite3.Connection) -> None:
    task = _task_with_run(conn)
    ledger.append_finding(
        conn, task.id, "拿到 flag{abc}", "首页回显", "raw/flag.txt", verify_state="verified"
    )
    verdict = exit_checklist.run_checklist(
        conn, task.id, acceptance=_acceptance(("AC-flag", "FLAG{"))
    )
    assert verdict.passed is True  # 子串匹配不分大小写


def test_acceptance_fails_when_only_claimed_matches(conn: sqlite3.Connection) -> None:
    """claimed 不算交付：没经裁判确认的"我拿到了"不能过出场。"""
    task = _task_with_run(conn)
    ledger.append_finding(conn, task.id, "拿到 flag{abc}", "d", "raw/flag.txt")
    verdict = exit_checklist.run_checklist(
        conn, task.id, acceptance=_acceptance(("AC-flag", "flag{"))
    )
    assert verdict.passed is False
    assert any("交付对目标" in f and "AC-flag" in f for f in verdict.failures)


def test_acceptance_fails_when_missing(conn: sqlite3.Connection) -> None:
    task = _task_with_run(conn)
    ledger.append_finding(
        conn, task.id, "22/tcp open ssh", "ssh 服务", "raw/a.txt", verify_state="verified"
    )
    verdict = exit_checklist.run_checklist(
        conn, task.id, acceptance=_acceptance(("AC-flag", "flag{"), ("AC-22", "22/tcp"))
    )
    assert verdict.passed is False
    assert any("AC-flag" in f for f in verdict.failures)
    assert not any("AC-22" in f for f in verdict.failures)  # 命中的条目不报


def test_no_acceptance_keeps_old_semantics(conn: sqlite3.Connection) -> None:
    """未配验收 = 不判（mock/探索型任务不因"没交付物"虚红）。"""
    task = _task_with_run(conn)
    assert exit_checklist.ExitChecklist().check(conn, task.id).passed is True


def test_checklist_fails_on_open_plan(conn: sqlite3.Connection) -> None:
    """规划未收口 = 不能出场（M5：提过的规划必须标完成或废弃）。"""
    task = _task_with_run(conn)
    plan.add(conn, task.id, 1, ["A：端口摸底"])
    verdict = exit_checklist.run_checklist(conn, task.id)
    assert verdict.passed is False
    assert any("规划未收口" in f for f in verdict.failures)


def test_checklist_passes_after_plan_closed(conn: sqlite3.Connection) -> None:
    task = _task_with_run(conn)
    plan.add(conn, task.id, 1, ["A：端口摸底"])
    plan.complete(conn, task.id, ["A：端口摸底"])
    assert exit_checklist.run_checklist(conn, task.id).passed is True


def test_unmet_acceptance_bounces_done_until_wall() -> None:
    """接线证明：验收未过时 done 被弹回，连败 3 轮按 E_WALL 走异常出口。"""
    from secpt.core.loop import PentestLoop
    from secpt.errors import SecptError
    from secpt.llms.main_llm import MainLlm
    from secpt.tools.registry import ToolRegistry

    conn = migrate.connect(":memory:")
    goal = json.dumps(
        {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"}, ensure_ascii=False
    )
    done = json.dumps({"thought": "收工", "plan_updates": [], "tool_calls": [], "done": True})
    fake = FakeLLM([goal, done, done, done])
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        ToolRegistry([]),
        exit_checker=exit_checklist.ExitChecklist(acceptance=_acceptance(("AC-flag", "flag{"))),
    )
    with pytest.raises(SecptError) as info:
        loop.run("交付演示 10.0.0.5")
    assert info.value.code == "E_WALL"
    assert ledger.latest_task(conn).exit_kind == "stalled"
    conn.close()


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
