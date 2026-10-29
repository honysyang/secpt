"""M5 双图事件线消费端测试：节拍器 / 配置口径 / 主循环步界重估。

三段分工：
1) PlanWatcher 纯测：只认 graph_changed、步号整除节拍才置旗、take 取走即清；
2) load_plan_config：缺省宽容（无文件/无段取默认）、坏值 E_PROFILE；
3) 主循环接线：事件线置旗 → 步界调 LLMC 重估 → audit(actor=llmc) 先账 →
   下一步注意力包吸收；名额用尽只落一次账；空白提案不占名额。
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pytest

from secpt.core.loop import PentestLoop
from secpt.core.plan_watch import PlanWatcher, load_plan_config
from secpt.errors import SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import LlmClient
from secpt.llms.coach import Coach, Proposal
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.ob.cli_view import CliView
from secpt.ob.events import EventBus
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.registry import ToolRegistry

# ---------- 节拍器纯测 ----------------------------------------------------------


class TestPlanWatcher:
    def test_flags_exactly_on_beat(self) -> None:
        watcher = PlanWatcher(every_steps=3)
        for step_no in (1, 2):
            watcher("graph_changed", {"step_no": step_no})
        assert watcher.take() is False
        watcher("graph_changed", {"step_no": 3})
        assert watcher.take() is True
        assert watcher.take() is False  # 取走即清：同一步不重复烧 LLM

    def test_ignores_other_events_and_bad_payload(self) -> None:
        watcher = PlanWatcher(every_steps=1)
        watcher("stall_alarm", {"step_no": 1})
        watcher("graph_changed", {"step_no": "1"})
        watcher("graph_changed", {})
        assert watcher.take() is False

    def test_zero_every_steps_rejected(self) -> None:
        with pytest.raises(ValueError):
            PlanWatcher(every_steps=0)


# ---------- 配置口径 ------------------------------------------------------------


class TestPlanConfig:
    def _write(self, tmp_path: Path, body: str) -> str:
        path = tmp_path / "profile.toml"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_defaults_when_missing(self, tmp_path: Path) -> None:
        assert load_plan_config(None).review_every_steps == 5
        assert load_plan_config(str(tmp_path / "absent.toml")).review_limit == 3
        cfg = load_plan_config(self._write(tmp_path, "target = 'x'\n"))
        assert (cfg.review_every_steps, cfg.review_limit) == (5, 3)

    def test_reads_plan_section(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "[plan]\nreview_every_steps = 2\nreview_limit = 1\n")
        cfg = load_plan_config(path)
        assert (cfg.review_every_steps, cfg.review_limit) == (2, 1)

    @pytest.mark.parametrize(
        "body",
        [
            "[plan]\nreview_every_steps = 0\n",
            "[plan]\nreview_limit = -1\n",
            "[plan]\nreview_every_steps = '5'\n",
            "[plan]\nreview_limit = true\n",
            "plan = 5\n",
        ],
    )
    def test_bad_values_reject_e_profile(self, tmp_path: Path, body: str) -> None:
        with pytest.raises(SecptError) as info:
            load_plan_config(self._write(tmp_path, body))
        assert info.value.code == "E_PROFILE"


# ---------- 主循环接线 ----------------------------------------------------------


def _goal_json() -> str:
    return json.dumps(
        {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"}, ensure_ascii=False
    )


def _step_json(thought: str, *, done: bool = False) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": [], "done": done},
        ensure_ascii=False,
    )


def _loop(
    fake: FakeLLM, *, every: int, limit: int = 3
) -> tuple[PentestLoop, EventBus, list[tuple[str, Mapping[str, object]]], sqlite3.Connection]:
    conn = migrate_store.connect(":memory:")
    bus = EventBus()
    events: list[tuple[str, Mapping[str, object]]] = []
    bus.subscribe(lambda name, payload: events.append((name, payload)))
    watcher = PlanWatcher(every_steps=every)  # 同一个实例：总线置旗、循环取旗
    bus.subscribe(watcher)
    client = LlmClient(fake)
    loop = PentestLoop(
        conn,
        MainLlm(client),
        ToolRegistry([]),
        bus=bus,
        exit_checker=ExitChecklist(),
        coach=Coach(client),
        plan_watcher=watcher,
        plan_review_limit=limit,
    )
    return loop, bus, events, conn


def test_review_fires_on_beat_and_is_absorbed_next_step() -> None:
    """节拍到点 → LLMC 重估（audit llmc 先账）→ 下一步注意力包携带提案。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("扫 1"),
            _step_json("扫 2"),
            _step_json("扫 3"),
            Proposal(idea="换打 443", reason="双图显示只有 22"),
            _step_json("听教练的，换打 443", done=True),
        ]
    )
    loop, bus, events, conn = _loop(fake, every=3)
    loop.run("规划重估演示 10.0.0.5")

    # 吸收断言：重估后第一个主 step 的注意力包携带提案文本（一次即用掉）。
    assert "教练规划重估待吸收（事件驱动，只提案不裁决）" in fake.calls[5][1].content
    assert "换打 443" in fake.calls[5][1].content
    llmc = [r["what"] for r in conn.execute("SELECT what FROM audit_log WHERE actor='llmc'")]
    assert len(llmc) == 1 and "规划重估" in llmc[0]
    reviewed = [payload for name, payload in events if name == "plan_reviewed"]
    assert reviewed and reviewed[0]["step_no"] == 3 and reviewed[0]["idea"] == "换打 443"
    assert ledger.latest_task(conn).exit_kind == "normal"


def test_review_limit_stops_llm_and_notes_once() -> None:
    """名额用尽：仍取旗但不再烧 LLMC，"用尽"只落一次账。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("扫 1"),
            Proposal(idea="换 443", reason="r1"),
            _step_json("扫 2"),
            _step_json("收工", done=True),
        ]
    )
    loop, _bus, _events, conn = _loop(fake, every=1, limit=1)
    loop.run("名额演示 10.0.0.5")

    assert fake.remaining == 0  # 只有一次教练调用（名额 1）
    whats = [r["what"] for r in conn.execute("SELECT what FROM audit_log WHERE actor='main'")]
    stops = [what for what in whats if "规划重估名额用尽" in what]
    assert len(stops) == 1


def test_blank_review_consumes_no_quota() -> None:
    """空白提案 = 规划仍成立：不落账、不占名额，下一次节拍仍可重估。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("扫 1"),
            Proposal(idea="  ", reason="现状够用"),
            _step_json("扫 2"),
            Proposal(idea="补打 8080", reason="发现新线索"),
            _step_json("收工", done=True),
        ]
    )
    loop, _bus, _events, conn = _loop(fake, every=1, limit=1)
    loop.run("空白提案演示 10.0.0.5")

    llmc = [r["what"] for r in conn.execute("SELECT what FROM audit_log WHERE actor='llmc'")]
    assert len(llmc) == 1 and "补打 8080" in llmc[0]


def test_review_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        PentestLoop(
            migrate_store.connect(":memory:"),
            MainLlm(LlmClient(FakeLLM([]))),
            ToolRegistry([]),
            exit_checker=ExitChecklist(),
            plan_review_limit=0,
        )


def test_cli_view_renders_plan_review_bubble() -> None:
    """CLI 角色气泡：plan_reviewed 事件渲染成【LLMC】行（可读性面）。"""
    out = io.StringIO()
    CliView(out=out)("plan_reviewed", {"step_no": 5, "idea": "换打 443"})
    assert "【LLMC】step 5 规划重估：换打 443" in out.getvalue()
