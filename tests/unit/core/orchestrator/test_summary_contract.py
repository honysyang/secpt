"""结构化加固：RunSummary/ChainResult 契约 + EVENT_CATALOG 目录。"""

from __future__ import annotations

import json


class TestRunSummaryContract:
    from typing import ClassVar

    CONTRACT_KEYS: ClassVar[set[str]] = {
        "job_id",
        "mode",
        "entries",
        "flags",
        "chains",
        "wall_s",
        "open_remaining",
        "warroom_assets",
        "warroom_attempts",
        "findings",
        "verified",
    }

    def test_defaults_cover_contract(self):
        from secpt.core.orchestrator.summary import RunSummary

        d = RunSummary(job_id="j1", mode="frontier").to_dict()
        assert set(d) >= self.CONTRACT_KEYS  # 缺省即"无此口径"，键从不缺席
        assert d["entries"] == [] and d["chains"] == [] and d["verified"] == 0

    def test_chain_result_unified_shape(self):
        """统一形态：单链与 frontier 链条目字段集一致（消费方免双态分支）。"""
        from secpt.core.orchestrator.summary import ChainResult

        single = ChainResult(role="exploit", entry=0, status="success", steps=10, wall_s=1.0)
        frontier = ChainResult(
            intent="利用 x", role="exploit", asset_scope="http://t", status="done"
        )
        assert set(single.to_dict()) == set(frontier.to_dict())
        assert frontier.to_dict()["entry"] == -1  # 缺省哨兵
        assert single.to_dict()["intent"] == ""

    def test_extras_flat_into_dict(self):
        from secpt.core.orchestrator.summary import RunSummary

        d = RunSummary(
            job_id="j", mode="frontier", extras={"sub_jobs": ["a"], "ledger": {}}
        ).to_dict()
        assert d["sub_jobs"] == ["a"] and "ledger" in d


class TestEventCatalog:
    def test_catalog_covers_all_types(self):
        from secpt.core.job.event import EVENT_CATALOG, EVENT_TYPES

        assert set(EVENT_CATALOG) == EVENT_TYPES  # 单点事实源：类型/目录必须同步

    def test_all_sink_hooks_satisfy_catalog(self, tmp_path):
        """演练全部 hook → 写出的每个事件 payload 都满足目录必备键。"""
        from secpt.core.job.event import EVENT_CATALOG, EventSink

        sink = EventSink("jc", tmp_path, store=None)
        sink.on_step(1, 0.1)
        sink.on_assistant({"content": "c", "tool_calls": []})
        sink.on_tool("c1", "bash", {"command": "ls"})
        sink.on_result("c1", "ok")
        sink.on_round_end(0, [{"role": "user", "content": "x"}])
        sink.status_change("running")
        sink.finding("f1", "flag", "FLAG{x}")
        sink.budget_warning("w")
        sink.error("e")
        sink.on_tokens(10, 5)
        sink.on_compaction(1, "s", 3, 1000)
        sink.on_compaction_requested(2, 3, 1000)
        sink.on_compaction_completed(2, "s", 3, 1000)
        sink.on_rollover(1, [{"role": "user", "content": "x"}], "h")
        sink.on_user_message("m", kind="steer")
        sink.write("routed", {"profile": "ctf", "via": "rule"})

        bad: list[str] = []
        for line in (tmp_path / "jc" / "events.jsonl").read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            missing = EVENT_CATALOG.get(ev["type"], frozenset()) - set(ev["payload"])
            if missing:
                bad.append(f"{ev['type']} 缺 {sorted(missing)}")
        assert bad == [], f"payload 不满足目录契约: {bad}"

    def test_missing_key_warns_once_not_raises(self, tmp_path, capsys):
        from secpt.core.job.event import EventSink

        sink = EventSink("jw", tmp_path, store=None)
        sink.write("step", {})  # 缺 n/elapsed_s
        sink.write("step", {})  # 第二次不再告警
        err = capsys.readouterr().err
        assert err.count("payload 缺键") == 1
        # 事件仍正常落盘（观测层不阻断）
        assert (tmp_path / "jw" / "events.jsonl").read_text(encoding="utf-8").strip()
