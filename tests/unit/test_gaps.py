"""短板补齐测试：结构化态势注入 / 事件驱动唤醒 / 指标快照趋势 / 幂等结算 / 多 provider 路由。"""

from __future__ import annotations

import asyncio

from secpt.core.orchestrator.frontier import Frontier, plan_round, run_frontier


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _resp(content):
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _R:
        def __init__(self, c):
            self.choices = [type("c", (), {"message": _Msg(c)})()]

    return _R(content)


class TestSituationReport:
    def test_structured_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.intel.store import IntelStore

        fr = Frontier(tmp_path / "f.db")
        intel = IntelStore(tmp_path / "intel.sqlite")
        intel.add_node("fact", "SAML 端点未校验签名")
        aid = intel.observe_asset("endpoint", "http://t/login")
        intel.add_node("fact", "测过登录", anchor_assets=[aid])
        # 一个未触达端点
        intel.observe_asset("endpoint", "http://t/untested")

        report = fr.situation_report()
        assert "open_intents" in report and "recent_results" in report
        assert any("SAML" in f_["content"] for f_ in report["recent_facts"])
        assert any("untested" in a["value"] for a in report["untested_assets"])

    def test_planner_prompt_contains_situation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.intel.store import IntelStore

        fr = Frontier(tmp_path / "f.db")
        IntelStore(tmp_path / "intel.sqlite").add_node("finding", "存在暴力破解面")
        seen = {}

        class _LLM:
            async def call(self, messages, tools, model):
                seen["prompt"] = messages[0]["content"]
                return _resp("[]")

        _run(plan_round("目标", fr, _LLM(), next_depth=1))
        assert "【图记忆·最新事实/发现" in seen["prompt"]
        assert "暴力破解" in seen["prompt"]
        assert "【覆盖度缺口" in seen["prompt"]


class TestEventDriven:
    def test_wake_on_settle_not_polling(self, tmp_path, monkeypatch):
        """事件驱动：planner 唤醒次数受 settle 事件驱动，不靠空轮询（心跳可控）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_FRONTIER_HEARTBEAT", "5")
        fr = Frontier(tmp_path / "f.db")
        fr.add("唯一任务", source="direct", priority=9)
        plans = []

        class _Planner:
            async def call(self, messages, tools, model):
                plans.append(1)
                return _resp("[]")

        from secpt.core.job.fakellm import FakeLLM

        summary = _run(
            run_frontier(
                "目标",
                llm=FakeLLM(n_steps=1, job_id="ev1", step_delay_s=0),
                planner_llm=_Planner(),
                frontier=fr,
                max_rounds=5,
                max_workers=1,
            )
        )
        # direct 模式：planner 全程未参与；任务被执行且收敛
        assert plans == []
        assert len(summary["chains"]) == 1 and summary["chains"][0]["status"] == "done"

    def test_parallel_workers_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_FRONTIER_HEARTBEAT", "5")
        fr = Frontier(tmp_path / "f.db")
        for i in range(3):
            fr.add(f"并行任务{i}", source="direct", priority=9)
        from secpt.core.job.fakellm import FakeLLM

        summary = _run(
            run_frontier(
                "目标",
                llm=FakeLLM(n_steps=1, job_id="pw", step_delay_s=0),
                planner_llm=None,
                frontier=fr,
                max_rounds=2,
                max_workers=3,
            )
        )
        assert len(summary["chains"]) == 3


class TestIdempotentSettle:
    def test_double_settle_safe(self, tmp_path):
        """跨库一致性补偿：重复结算同一意图结果稳定（last-write-wins，无副作用）。"""
        fr = Frontier(tmp_path / "f.db")
        iid = fr.add("x", source="direct")
        fr.settle(iid, status="done", result="r1")
        fr.settle(iid, status="done", result="r1")  # 重放
        rows = [r for r in fr.list_all() if r["id"] == iid]
        assert len(rows) == 1 and rows[0]["status"] == "done"


class TestMetricsSnapshot:
    def test_snapshot_and_trend(self, tmp_path, monkeypatch):
        from secpt.cli.cmd_ops import metrics_cmd

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.findings.store import FindingStore

        fs = FindingStore(tmp_path / "f.db")
        import contextlib
        import io

        # 第一轮：0 confirmed → 快照
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            metrics_cmd(fmt="json", snapshot=True, _finding_store=fs)
        # 第二轮：1 confirmed → 快照 + 趋势
        from secpt.findings.models import Finding

        f = fs.save(Finding(title="X", target="t"))
        fs.record_reproduction(f.id, job_id="v", verdict="reproduced")
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            metrics_cmd(fmt="table", snapshot=True, trend=1, _finding_store=fs)
        out = buf2.getvalue()
        assert "趋势" in out and "confirmed" in out and "0 → 1" in out
        hp = tmp_path / "metrics_history.jsonl"
        assert hp.is_file() and len(hp.read_text().strip().splitlines()) == 2


class TestMultiProviderRouting:
    def test_two_providers_and_pool(self, tmp_path, monkeypatch):
        """多 provider 实战演练（mock 传输层）：池成员各自走自己的端点解析。"""
        from secpt.runtime.llm import pool
        from secpt.runtime.llm.client import resolve_model

        monkeypatch.setenv("SECPT_MODEL_BASE_GW1", "https://gw1.example/v1")
        monkeypatch.setenv("SECPT_MODEL_KEY_GW1", "k1")
        monkeypatch.setenv("SECPT_MODEL_BASE_GW2", "https://gw2.example/v1")
        monkeypatch.setenv("SECPT_MODEL_KEY_GW2", "k2")
        monkeypatch.setenv("SECPT_MODEL_POOL", "gw1/m-a,gw2/m-b")

        assert resolve_model("gw1/m-a")[:2] == ("https://gw1.example/v1", "k1")
        assert resolve_model("gw2/m-b")[:2] == ("https://gw2.example/v1", "k2")
        assert pool.pool_models() == ["gw1/m-a", "gw2/m-b"]

        calls = []

        async def fake_call_llm(messages, tools, model, api_key=None, fallback=None):
            calls.append(model)
            if model == "gw1/m-a":
                raise ConnectionError("gw1 down")
            return _resp("ok")

        monkeypatch.setattr(pool, "call_llm", fake_call_llm)
        out = _run(pool.call_pooled([], [], "gw1/m-a"))
        assert out.choices[0].message.content == "ok"
        assert calls == ["gw1/m-a", "gw2/m-b"]  # 故障转移到第二个 provider
        pool._circuits.clear()
