"""M5-7 关键参数与重复抑制测试：配置口径 / 失速双口径 / 温度档位。

三段（设计文档 L66 关键参数、L84 重复抑制、L83 温度分档）：
1) load_limits_config：缺省宽容、覆盖生效、坏值 E_PROFILE；
2) StallCounter.observe_plan：连续同一规划拉铃、换规划/空 updates 清零、reset 清双计；
3) 主循环接线：重复规划 → E_STALL（stalled）；规划有变化不误拉铃；
   Coach.propose 走发散档 0.8 / review 走规划档 0.2。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from secpt.core.limits import load_limits_config
from secpt.core.loop import PentestLoop
from secpt.core.stall import StallCounter
from secpt.errors import SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import TEMPERATURE_FREESTYLE, TEMPERATURE_PLAN, LlmClient, Message
from secpt.llms.coach import Coach
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.registry import ToolRegistry

# ---------- 配置口径 ------------------------------------------------------------


class TestLimitsConfig:
    def _write(self, tmp_path: Path, body: str) -> str:
        path = tmp_path / "profile.toml"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_defaults(self, tmp_path: Path) -> None:
        cfg = load_limits_config(None)
        assert (cfg.max_steps, cfg.stall_failures, cfg.stall_repeat_plans) == (200, 5, 2)
        assert load_limits_config(str(tmp_path / "absent.toml")).max_steps == 200
        assert load_limits_config(self._write(tmp_path, "target = 'x'\n")).stall_failures == 5

    def test_reads_sections(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path, "[budget]\nmax_steps = 7\n[stall]\nfailures = 3\nrepeat_plans = 4\n"
        )
        cfg = load_limits_config(path)
        assert (cfg.max_steps, cfg.stall_failures, cfg.stall_repeat_plans) == (7, 3, 4)

    @pytest.mark.parametrize(
        "body",
        [
            "[budget]\nmax_steps = 0\n",
            "[budget]\nmax_steps = -1\n",
            "[budget]\nmax_steps = true\n",
            "[stall]\nfailures = '5'\n",
            "[stall]\nrepeat_plans = 0\n",
            "budget = 200\n",
        ],
    )
    def test_bad_values_reject_e_profile(self, tmp_path: Path, body: str) -> None:
        with pytest.raises(SecptError) as info:
            load_limits_config(self._write(tmp_path, body))
        assert info.value.code == "E_PROFILE"


# ---------- 失速计双口径 --------------------------------------------------------


class TestStallRepeat:
    def test_same_plan_twice_rings(self) -> None:
        counter = StallCounter(repeat_n=2)
        assert counter.observe_plan(["A：摸底"]) is False
        assert counter.observe_plan(["A：摸底"]) is True  # 连续两轮同一规划

    def test_changed_plan_resets_streak(self) -> None:
        counter = StallCounter(repeat_n=2)
        assert counter.observe_plan(["A"]) is False
        assert counter.observe_plan(["B"]) is False
        assert counter.observe_plan(["B"]) is True

    def test_empty_updates_never_ring(self) -> None:
        counter = StallCounter(repeat_n=2)
        for _ in range(5):
            assert counter.observe_plan([]) is False
            assert counter.observe_plan(["  "]) is False

    def test_whitespace_normalized(self) -> None:
        counter = StallCounter(repeat_n=2)
        assert counter.observe_plan([" A ：摸底 "]) is False
        assert counter.observe_plan(["A ：摸底"]) is True

    def test_reset_clears_both_counters(self) -> None:
        counter = StallCounter(n=2, repeat_n=2)
        counter.observe(ok=False)
        counter.observe_plan(["A"])
        counter.reset()
        assert counter.observe(ok=False) is False
        assert counter.observe_plan(["A"]) is False  # 救场后重新起算

    def test_bad_thresholds_rejected(self) -> None:
        with pytest.raises(ValueError):
            StallCounter(repeat_n=0)


# ---------- 主循环接线 ----------------------------------------------------------


def _goal_json() -> str:
    return json.dumps(
        {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"}, ensure_ascii=False
    )


def _step_json(thought: str, *, plans: list[str] | None = None, done: bool = False) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": plans or [], "tool_calls": [], "done": done},
        ensure_ascii=False,
    )


def _loop(fake: FakeLLM, *, repeat_plans: int = 2) -> tuple[PentestLoop, sqlite3.Connection]:
    conn = migrate_store.connect(":memory:")
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        ToolRegistry([]),
        exit_checker=ExitChecklist(),
        stall_repeat_plans=repeat_plans,
    )
    return loop, conn


def test_repeated_plan_rings_and_stalls() -> None:
    """连续两轮同一规划 → 拉铃 → 无教练 → E_STALL（stalled），不悬死。"""
    fake = FakeLLM(
        [_goal_json(), _step_json("老路", plans=["A：摸底"]), _step_json("老路", plans=["A：摸底"])]
    )
    loop, conn = _loop(fake)
    with pytest.raises(SecptError) as info:
        loop.run("重复抑制演示 10.0.0.5")
    assert info.value.code == "E_STALL"
    assert ledger.latest_task(conn).exit_kind == "stalled"
    assert fake.call_count == 3


def test_changing_plan_does_not_ring() -> None:
    """规划有变化 = 还在推进：不误拉铃，正常收工。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("第一步", plans=["A：摸底"]),
            _step_json("第二步", plans=["B：初筛"]),
            _step_json("收工", done=True),
        ]
    )
    loop, conn = _loop(fake)
    loop.run("换规划演示 10.0.0.5")
    assert ledger.latest_task(conn).exit_kind == "normal"


# ---------- 温度档位 ------------------------------------------------------------


class _RecordingProvider:
    """记录每次调用温度的 provider 替身（温度是协议层透传，档位由器官选）。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.temps: list[float] = []

    def complete(self, messages: Sequence[Message], *, temperature: float, timeout_s: float) -> str:
        self.temps.append(temperature)
        return self._payload


def _proposal_json() -> str:
    return json.dumps({"idea": "换打 443", "reason": "22 走死"}, ensure_ascii=False)


def test_coach_propose_uses_freestyle_temperature() -> None:
    """撞墙换路走发散档 0.8（设计文档 L83）。"""
    provider = _RecordingProvider(_proposal_json())
    proposal = Coach(LlmClient(provider)).propose("节点 1", "规划 v1")
    assert proposal is not None
    assert provider.temps == [TEMPERATURE_FREESTYLE]


def test_coach_review_uses_plan_temperature() -> None:
    """规划重估是核对现状，保持规划档低温 0.2。"""
    provider = _RecordingProvider(_proposal_json())
    proposal = Coach(LlmClient(provider)).review("节点 1", "规划 v1")
    assert proposal is not None
    assert provider.temps == [TEMPERATURE_PLAN]


def test_loop_rejects_bad_stall_thresholds() -> None:
    with pytest.raises(ValueError):
        PentestLoop(
            migrate_store.connect(":memory:"),
            MainLlm(LlmClient(FakeLLM([]))),
            ToolRegistry([]),
            exit_checker=ExitChecklist(),
            stall_repeat_plans=0,
        )
