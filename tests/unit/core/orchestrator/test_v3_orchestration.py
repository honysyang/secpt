"""v3 orchestration: triage / coach / delivery gate / lanes / retro."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from secpt.core.orchestrator import frontier as _fr
from secpt.core.orchestrator.coach import coach_advice, coach_for_retry
from secpt.core.orchestrator.delivery_gate import review_delivery
from secpt.core.orchestrator.frontier import (
    Frontier,
    Intent,
    _make_delegate_logistics_tool,
    frontier_db_path,
    run_frontier,
)
from secpt.core.orchestrator.triage import quick_check_result, triage_task


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _EmptyPlanner:
    async def call(self, messages, tools, model):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
    monkeypatch.setenv("SECPT_ORCH_DELIVERY", "0")
    monkeypatch.setenv("SECPT_ORCH_LOGISTICS", "0")
    return tmp_path


# ---------------------------------------------------------------------------
# triage
# ---------------------------------------------------------------------------


def test_triage_logistics_and_verification() -> None:
    background = triage_task("下载工具并批量搜索子域名", "准备字典", "下载完成")
    assert background.lane == "logistics"
    assert "后勤" in background.reason

    verify = triage_task("复验 flag", "独立复现 FLAG{x}", "复现同一 flag")
    assert verify.lane == "main"  # 验证工作永远走主路径
    assert verify.needs_delivery

    normal = triage_task("分析登录接口", "找 SQL 注入", "确认一个漏洞")
    assert normal.lane == "main"


def test_quick_check_requires_real_flag() -> None:
    ok, why = quick_check_result("我拿到了 flag", "拿到 flag")
    assert not ok and "flag" in why.lower()
    ok, _ = quick_check_result("ANSWER: FLAG{abc123}", "拿到 flag")
    assert ok
    ok, _ = quick_check_result("结论：没有漏洞", "确认漏洞或明确否定")
    assert ok


# ---------------------------------------------------------------------------
# coach
# ---------------------------------------------------------------------------


def test_coach_handles_stalled_and_budget() -> None:
    stalled = coach_advice("打边界", status="stalled", fail_reason="无进展", attempts=1)
    assert stalled.kind == "reset" and "教练介入" in stalled.note
    budget = coach_advice("大扫描", status="step_exceeded", fail_reason="步数耗尽", attempts=1)
    assert budget.kind == "shrink" and "最小" in budget.note
    intent = Intent(
        id="i1",
        title="x",
        goal="",
        done_criteria="",
        priority=5,
        depth=0,
        status="open",
        attempts=2,
        fail_reason="timeout",
    )
    assert "教练介入" in coach_for_retry(intent)


# ---------------------------------------------------------------------------
# delivery gate
# ---------------------------------------------------------------------------


def test_delivery_gate_requires_verification_then_accepts(env) -> None:
    fr = Frontier(Path(env) / "gate.sqlite")
    fr.add("战果", done_criteria="拿到 flag")
    iid = fr.claim_next()
    fr.settle(iid.id, status="done", result="FLAG{gate}")

    first = review_delivery(fr, base="b", objective="目标", flags=["FLAG{gate}"], attempt=0)
    assert not first.accepted and first.followups
    assert "复验" in first.followups[0]["title"]

    fid = fr.add(
        first.followups[0]["title"],
        goal=first.followups[0]["goal"],
        source="delivery",
        lane="main",
    )
    fr.claim_next()
    fr.settle(fid, status="done", result="独立复现：FLAG{gate}")
    second = review_delivery(fr, base="b", objective="目标", flags=["FLAG{gate}"], attempt=1)
    assert second.accepted
    assert second.verified_flags == ["FLAG{gate}"]


def test_delivery_gate_negative_result_one_followup(env) -> None:
    fr = Frontier(Path(env) / "gate2.sqlite")
    first = review_delivery(fr, base="b2", objective="目标", flags=[], attempt=0)
    assert not first.accepted and first.followups
    second = review_delivery(fr, base="b2", objective="目标", flags=[], attempt=1)
    assert second.accepted


# ---------------------------------------------------------------------------
# lanes / local delegation
# ---------------------------------------------------------------------------


def test_lane_claim_isolation_and_delegate_tool(env) -> None:
    fr = Frontier(Path(env) / "lanes.sqlite")
    main_id = fr.add("主任务")
    log_id = fr.add("后勤任务", lane="logistics")
    assert fr.count_open(lane="main") == 1
    assert fr.count_open(lane="logistics") == 1
    assert fr.claim_next(lane="main").id == main_id
    assert fr.claim_next(lane="logistics").id == log_id
    assert fr.claim_next(lane="main") is None

    parent = Intent(
        id="p1",
        title="打目标",
        goal="拿 flag",
        done_criteria="flag",
        priority=9,
        depth=0,
        status="running",
        attempts=1,
        lane="main",
        asset_scope="http://target",
    )
    tool = _make_delegate_logistics_tool(fr, "b", parent)["delegate_logistics"][1]
    msg = tool({"task": "下载字典并批量搜索", "why": "长耗时"})
    assert "已交给后勤" in msg
    rows = fr.list_all(status="open")
    log = [r for r in rows if r["lane"] == "logistics"]
    assert len(log) == 1
    assert "打目标" in log[0]["goal"]


# ---------------------------------------------------------------------------
# integration: delivery gate + retro
# ---------------------------------------------------------------------------


def test_delivery_gate_integration_and_retro(env, monkeypatch) -> None:
    monkeypatch.setenv("SECPT_ORCH_DELIVERY", "1")
    calls: list[str] = []

    def fake_run_engine(**kw):
        calls.append(str(kw.get("job_id")))
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag="FLAG{delivered}",
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "deliver-int"
    fr = Frontier(frontier_db_path(base))
    fr.add("打目标", goal="拿 flag", done_criteria="拿到 flag", priority=9)
    summary = _run(
        run_frontier(
            "目标",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_EmptyPlanner(),
            max_rounds=3,
            max_intents=5,
            stop_on_first_flag=True,
            enable_delivery=True,
            enable_logistics=False,
        )
    )
    assert len(calls) == 2  # 原任务 + 交付复验
    assert summary["delivery"]["accepted"] is True
    assert summary["flags"] == ["FLAG{delivered}"]
    assert summary["retro_path"]
    assert Path(summary["retro_path"]).is_file()
    assert "交付判定" in Path(summary["retro_path"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# integration: background logistics does not block main task
# ---------------------------------------------------------------------------


def test_logistics_worker_runs_in_background(env, monkeypatch) -> None:
    seen: list[str] = []

    def fake_run_engine(**kw):
        obj = str(kw.get("objective") or "")
        seen.append(kw.get("job_id"))
        if "后台" in obj or "后勤" in obj:
            return SimpleNamespace(
                job_id=kw.get("job_id"),
                flag=None,
                steps=1,
                wall_s=0.01,
                status="success",
                messages=[],
            )
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag="FLAG{main}",
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "logi-int"
    fr = Frontier(frontier_db_path(base))
    fr.add("主任务", goal="拿 flag", done_criteria="拿到 flag", priority=9)
    fr.add("后台下载字典", goal="后台执行下载", lane="logistics")
    summary = _run(
        run_frontier(
            "目标",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_EmptyPlanner(),
            max_rounds=3,
            max_intents=5,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=True,
        )
    )
    lanes = {c["lane"] for c in summary["chains"]}
    assert lanes == {"main", "logistics"}
    assert any(c["intent"] == "主任务" for c in summary["chains"])
    assert any(c["intent"] == "后台下载字典" for c in summary["chains"])
    assert len(seen) >= 2


# ---------------------------------------------------------------------------
# planner: generic prompt ignores role / triage sets lane
# ---------------------------------------------------------------------------


def test_plan_round_generic_ignores_roles_and_routes_logistics(env, monkeypatch) -> None:
    monkeypatch.setenv("SECPT_ROLE_TEMPLATES", "0")
    fr = Frontier(Path(env) / "plan.sqlite")
    payload = json.dumps(
        [
            {
                "title": "下载字典并批量搜索",
                "goal": "后台准备字典",
                "done_criteria": "下载完成",
                "priority": 5,
                "role": "recon",
                "lane": "logistics",
            }
        ],
        ensure_ascii=False,
    )

    class Planner:
        async def call(self, messages, tools, model):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
            )

    n = _run(_fr.plan_round("目标", fr, Planner(), model="m"))
    assert n == 1
    rows = fr.list_all()
    assert rows[0]["role"] == ""  # 角色模板默认关闭
    assert rows[0]["lane"] == "logistics"
    assert rows[0]["depends_on"] == ""
