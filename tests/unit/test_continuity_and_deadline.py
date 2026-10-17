"""M1/M3 通用能力测试：同资产跨意图交接、意图墙钟、全局截止排空、失败升级模型。

这些机制不只为跑分：recon→exploit→verify、多轮重试、长任务时间盒、0day 硬目标
升级模型都走同一条通用路径。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from secpt.core.job.resume import jobs_dir
from secpt.core.orchestrator import benchmark as bm
from secpt.core.orchestrator.frontier import (
    Frontier,
    _pick_intent_model,
    frontier_db_path,
    run_frontier,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestAssetContinuity:
    def test_asset_state_roundtrip(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        assert fr.asset_state("a-1") is None
        fr.record_asset_state("a-1", job_id="j1", status="done", summary="入口 admin/x 有效")
        st = fr.asset_state("a-1")
        assert st["last_job_id"] == "j1" and "admin/x" in st["summary"]
        fr.record_asset_state("a-1", job_id="j2", status="exhausted", summary="换路线")
        assert fr.asset_state("a-1")["last_job_id"] == "j2"  # upsert 覆盖

    def test_handoff_artifacts_and_objective(self, tmp_path, monkeypatch):
        """第二个同资产意图：objective 带交接块 + 工作区已搬运前序产物。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        old_ws = jobs_dir() / "j1" / "ws"
        old_ws.mkdir(parents=True)
        (old_ws / "exploit.py").write_text("payload", encoding="utf-8")

        fr = Frontier(frontier_db_path("cont1"))
        fr.record_asset_state("t-1", job_id="j1", status="done", summary="入口 /login 可注入")
        fr.add("第一阶段", done_criteria="x", asset_scope="t-1", priority=10, max_steps=5)
        fr.add("第二阶段", done_criteria="y", asset_scope="t-1", priority=9, max_steps=5)

        seen: list[dict] = []

        def fake_run_engine(**kw):
            seen.append(kw)
            jid = kw["job_id"]
            ws = jobs_dir() / jid / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "result.txt").write_text("ok", encoding="utf-8")
            return SimpleNamespace(
                job_id=jid, flag=None, steps=1, wall_s=0.01, status="failed", messages=[]
            )

        monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
        _run(
            run_frontier(
                "目标",
                profile="benchmark",
                base_job_id="cont1",
                frontier=fr,
                llm=object(),
                planner_llm=object(),
                max_intents=2,
                max_rounds=2,
                max_workers=1,
                stop_on_first_flag=False,
            )
        )
        assert any("同资产前序工作交接" in kw["objective"] for kw in seen)
        # 后续意图的工作区里能看到上一意图的 exploit.py
        assert any((jobs_dir() / kw["job_id"] / "ws" / "exploit.py").is_file() for kw in seen)


class TestIntentWallBudget:
    def test_intent_wall_passed_to_engine(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        fr = Frontier(frontier_db_path("wall1"))
        fr.add("定时意图", done_criteria="x", max_steps=5, max_wall_s=77)
        seen: list[dict] = []

        def fake_run_engine(**kw):
            seen.append(kw)
            return SimpleNamespace(
                job_id=kw["job_id"], flag=None, steps=1, wall_s=0.01, status="failed", messages=[]
            )

        monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
        _run(
            run_frontier(
                "目标",
                profile="benchmark",
                base_job_id="wall1",
                frontier=fr,
                llm=object(),
                planner_llm=object(),
                max_intents=1,
                max_rounds=1,
                max_workers=1,
            )
        )
        assert seen and seen[0]["max_wall_s"] == 77
        assert seen[0]["max_steps"] == 5

    def test_max_wall_roundtrip_through_claim(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        fr.add("t", max_steps=3, max_wall_s=120, asset_scope="a")
        it = fr.claim_next()
        assert it is not None and it.max_wall_s == 120 and it.max_steps == 3


class TestWallHardStop:
    def test_wall_exceeded_no_recheck(self, tmp_path, monkeypatch):
        """墙钟硬止损不进自动复核（否则换新预算再跑，时间盒失效）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        fr = Frontier(frontier_db_path("wh1"))
        fr.add("时间盒意图", done_criteria="x", max_wall_s=10)
        calls: list[str] = []

        def fake_run_engine(**kw):
            calls.append(str(kw["job_id"]))
            return SimpleNamespace(
                job_id=kw["job_id"],
                flag=None,
                steps=3,
                wall_s=10.01,
                status="wallclock_exceeded",
                messages=[],
            )

        monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
        _run(
            run_frontier(
                "目标",
                profile="benchmark",
                base_job_id="wh1",
                frontier=fr,
                llm=object(),
                planner_llm=object(),
                max_intents=1,
                max_rounds=1,
                max_workers=1,
            )
        )
        assert len(calls) == 1  # 不重跑
        rows = fr.list_all(status="exhausted")
        assert rows and rows[0]["attempts"] == 1


class TestDeadlineDrain:
    def test_past_deadline_stops_claiming(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        called: list[dict] = []

        def fake_run_engine(**kw):
            called.append(kw)
            return SimpleNamespace(
                job_id="x", flag=None, steps=1, wall_s=0.01, status="done", messages=[]
            )

        monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
        fr = Frontier(frontier_db_path("dl1"))
        fr.add("待办", done_criteria="x", max_steps=5)
        _run(
            run_frontier(
                "目标",
                profile="benchmark",
                base_job_id="dl1",
                frontier=fr,
                llm=object(),
                planner_llm=object(),
                max_intents=1,
                max_rounds=1,
                deadline_ts=time.time() - 1,
            )
        )
        assert called == []


class TestEscalationModel:
    def test_escalate_on_retry(self, monkeypatch):
        monkeypatch.setenv("SECPT_ESCALATE_MODEL", "strong-model")
        monkeypatch.setenv("SECPT_VERIFY_MODEL", "verifier-model")
        assert _pick_intent_model("", "base", attempts=1) == "base"
        assert _pick_intent_model("", "base", attempts=2) == "strong-model"
        # verify 角色优先异构验证模型（即便重试）
        assert _pick_intent_model("verify", "base", attempts=2) == "verifier-model"

    def test_no_escalation_env_keeps_default(self, monkeypatch):
        monkeypatch.delenv("SECPT_ESCALATE_MODEL", raising=False)
        assert _pick_intent_model("", "base", attempts=3) == "base"


class TestFamilyMetrics:
    def test_aggregate(self):
        rows = {
            "e1-01": {"flags_found": 1, "flags_total": 1, "completed": True},
            "e1-02": {"flags_found": 0, "flags_total": 2, "completed": False},
            "b-01": {"flags_found": 2, "flags_total": 4, "completed": False},
        }
        fams = bm._family_metrics(rows)
        assert fams["e1"] == {"n": 2, "flags": 1, "max_flags": 3, "completed": 1}
        assert fams["b"] == {"n": 1, "flags": 2, "max_flags": 4, "completed": 0}
