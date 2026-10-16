"""三档止损状态机测试（设计 docs/设计-工作循环形态.md §2，P1 验收口径）。

验收点：
- FakeLLM 空转脚本：nudge→wrap→stall 三跳可测，且 stalled 优雅退出（落 transcript、
  不抛异常、没烧满步数预算）；
- 收尾指令是 append-only user 消息（红线 1），工具/治理不受档位影响；
- SECPT_LOOP_STOP=0 一键回退旧行为（nudge 照常，永不 wrap/stall）；
- NUDGED→RUNNING 回退：nudge 后出现结构性新信息则不升级收尾；
- budget_pct 收尾：步数到 SECPT_WRAP_AT_PCT 注入收尾指令，模型可拿 ANSWER 获救。
"""

from __future__ import annotations

import asyncio
import json

from secpt.core.job.fakellm import FakeLLM, _resp
from secpt.runtime.engine import Engine, RunConfig


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cfg(tmp_path, job_id: str, max_steps: int = 200, **kw) -> RunConfig:
    base = dict(
        job_id=job_id,
        objective="测试三档止损状态机",
        system_extra="test",
        workspace=tmp_path,
        max_steps=max_steps,
        max_wall_s=300,
    )
    base.update(kw)
    return RunConfig(**base)


def _user_text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages if m["role"] == "user")


class _ScriptedLLM:
    """按脚本产出工具调用的假 LLM：先空转（静态输出），再按要求切换行为。"""

    def __init__(self, script):
        # script: list[dict] 逐项消费；末项重复。每项 {"command": ..., "answer": ...}
        self._script = list(script)
        self.calls = 0

    async def call(self, messages, tools, model):
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if item.get("answer"):
            return _resp({"role": "assistant", "content": item["answer"]})
        return _resp(
            {
                "role": "assistant",
                "content": f"scripted step {self.calls}",
                "tool_calls": [
                    {
                        "id": f"s-{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps({"command": item["command"]}),
                        },
                    }
                ],
            }
        )


class TestThreeTierStop:
    def test_nudge_wrap_stall_three_hop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_LOOP_STOP", "1")
        monkeypatch.setenv("SECPT_WRAP_AFTER_NUDGE", "2")
        monkeypatch.setenv("SECPT_STALL_AFTER_WRAP", "2")
        engine = Engine(
            _cfg(tmp_path, "loop1", stuck_threshold=3, stuck_wait_cap=4),
            llm=FakeLLM(n_steps=99999, job_id="loop1", step_delay_s=0),
        )
        result = _run(engine.run())
        assert result.status == "stalled"  # 三跳终点：STALLED
        assert result.steps < 200  # 没烧满硬预算墙（体面收场）
        # 优雅退出：transcript 正常落盘、消息链完整
        assert (tmp_path / "ws" / "transcript.json").is_file()
        assert any("收尾阶段" in str(m.get("content") or "") for m in result.messages)
        # nudge 确实注入过（四类触发器文案统一以"你已经"开头）
        assert any("你已经" in str(m.get("content") or "") for m in result.messages)

    def test_loop_stop_disabled_falls_back(self, tmp_path, monkeypatch):
        """SECPT_LOOP_STOP=0：回退旧行为——nudge 照常，永不 wrap/stall。"""
        monkeypatch.setenv("SECPT_LOOP_STOP", "0")
        engine = Engine(
            _cfg(tmp_path, "loop2", max_steps=12, stuck_threshold=2, stuck_wait_cap=2),
            llm=FakeLLM(n_steps=99999, job_id="loop2", step_delay_s=0),
        )
        result = _run(engine.run())
        assert result.status == "step_exceeded"  # 旧行为：硬预算墙收口
        assert "收尾阶段" not in _user_text(result.messages)

    def test_nudged_falls_back_on_improvement(self, tmp_path, monkeypatch):
        """NUDGED→RUNNING 回退：nudge 后出现结构性新信息则不升级收尾。"""
        monkeypatch.setenv("SECPT_LOOP_STOP", "1")
        monkeypatch.setenv("SECPT_WRAP_AFTER_NUDGE", "2")
        monkeypatch.setenv("SECPT_STALL_AFTER_WRAP", "2")
        monkeypatch.setenv("SECPT_WRAP_AT_PCT", "200")  # 本测只验证 nudge 回退，关掉步数收尾腿
        # 3 步静态空转触发 nudge（dup 触发器，cap=2），第 4 步起每步新信息 → 回退
        script = [{"command": "ls"}] * 3 + [{"command": f"echo uniq-{i}"} for i in range(30)]
        engine = Engine(
            _cfg(tmp_path, "loop3", max_steps=20, stuck_threshold=2, stuck_wait_cap=3),
            llm=_ScriptedLLM(script),
        )
        result = _run(engine.run())
        # 持续改善 → 永远不进 WRAPPING；nudge 本身已有（前 8 步空转）
        assert "收尾阶段" not in _user_text(result.messages)
        assert "你已经" in _user_text(result.messages)  # nudge 确实发生过

    def test_wrap_at_budget_pct_and_answer_rescue(self, tmp_path, monkeypatch):
        """步数到 SECPT_WRAP_AT_PCT 注入收尾指令；模型随后 ANSWER 可获救（非 stalled）。"""
        monkeypatch.setenv("SECPT_LOOP_STOP", "1")
        monkeypatch.setenv("SECPT_WRAP_AT_PCT", "50")
        script = [{"command": f"echo probe-{i}"} for i in range(60)]
        llm = _ScriptedLLM(script)

        async def _call(messages, tools, model):
            if any("收尾阶段" in str(m.get("content") or "") for m in messages):
                return _resp({"role": "assistant", "content": "ANSWER: FLAG{rescued}"})
            return await llm.call(messages, tools, model)

        class _LLM:
            call = staticmethod(_call)

        engine = Engine(_cfg(tmp_path, "loop4", max_steps=40), llm=_LLM())
        result = _run(engine.run())
        assert "收尾阶段" in _user_text(result.messages)
        assert result.status == "success"
        assert result.flag == "FLAG{rescued}"
        assert result.steps < 40  # 在收尾档拿到 ANSWER，没烧满预算


class TestLoopPhaseEvent:
    def test_loop_phase_events_written(self, tmp_path, monkeypatch):
        """loop_phase 事件登记 EVENT_CATALOG 且经 EventSink 落流（nudged/wrapping/stalled）。"""
        from secpt.core.job.event import EVENT_CATALOG, EVENT_TYPES

        assert "loop_phase" in EVENT_TYPES
        assert EVENT_CATALOG["loop_phase"] == frozenset({"phase"})

        monkeypatch.setenv("SECPT_LOOP_STOP", "1")
        monkeypatch.setenv("SECPT_WRAP_AFTER_NUDGE", "2")
        monkeypatch.setenv("SECPT_STALL_AFTER_WRAP", "2")
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))  # 隔离：事件流不进仓库 var/
        from secpt.core.job.event import EventSink
        from secpt.core.job.resume import jobs_dir

        sink = EventSink("loop5", jobs_dir())
        engine = Engine(
            _cfg(tmp_path, "loop5", stuck_threshold=3, stuck_wait_cap=4),
            llm=FakeLLM(n_steps=99999, job_id="loop5", step_delay_s=0),
            hooks=sink,
        )
        result = _run(engine.run())
        assert result.status == "stalled"
        phases = [
            json.loads(line)["payload"]["phase"]
            for line in sink.events_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["type"] == "loop_phase"
        ]
        assert "nudged" in phases and "wrapping" in phases and "stalled" in phases
