"""PA4 终止与上限精细化：

- PA4.1 tool-call arguments JSON 解析失败 → 不执行、回填自救错误串、
  连续 ≥2 次追加一次自救 user 消息（episode 内幂等，成功清零）
- PA4.2 工具调用硬上限（cfg.max_tool_calls）→ 超限强制进入 wrapping
  （注入 WRAP_MSG，走既有收尾通道）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secpt.runtime.engine import Engine, RunConfig


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, tc_id: str, name: str, arguments: str) -> None:
        self.id = tc_id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content: str, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None


class _Resp:
    def __init__(self, msg: _Msg) -> None:
        self.choices = [type("c", (), {"message": msg})()]


class ScriptedLLM:
    """按脚本逐轮返回响应。"""

    def __init__(self, responses: list[_Msg]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def call(self, messages: list, tools: list, model: str) -> _Resp:
        self.calls += 1
        return _Resp(self._responses.pop(0))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SECPT_DELEGATE", raising=False)
    monkeypatch.delenv("SECPT_TOOL_POOL", raising=False)
    monkeypatch.setenv("SECPT_AUTO_STRUCTURE", "0")


def _mk_engine(tmp_path: Path, llm, max_tool_calls: int = 0) -> Engine:
    cfg = RunConfig(
        job_id="pa4",
        objective="test",
        system_extra="",
        workspace=tmp_path,
        max_steps=10,
        max_tool_calls=max_tool_calls,
    )
    executed: list[dict] = []

    def read_file(args: dict) -> str:
        executed.append(args)
        return "data"

    eng = Engine(cfg, llm=llm, tools={"read_file": read_file})
    eng._executed = executed  # 测试断言用
    return eng


def _run(eng: Engine):
    return asyncio_run(eng.run())


def asyncio_run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


class TestParseRescue:
    def test_two_consecutive_failures_trigger_single_rescue(self, tmp_path):
        llm = ScriptedLLM(
            [
                _Msg("", [_TC("t1", "read_file", "{bad json")]),
                _Msg("", [_TC("t2", "read_file", "still not json")]),
                _Msg("ANSWER: done", []),
            ]
        )
        eng = _mk_engine(tmp_path, llm)
        result = _run(eng)
        assert result.status == "success"
        # 两次调用都未执行、都回填自救错误串
        assert eng._executed == []
        tool_msgs = [m for m in result.messages if isinstance(m, dict) and m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        assert all("不是合法 JSON" in m["content"] for m in tool_msgs)
        # 自救 user 消息恰好一次（episode 幂等）
        rescues = [
            m
            for m in result.messages
            if isinstance(m, dict) and m.get("role") == "user" and "自救提示" in m["content"]
        ]
        assert len(rescues) == 1

    def test_success_resets_streak_no_rescue(self, tmp_path):
        llm = ScriptedLLM(
            [
                _Msg("", [_TC("t1", "read_file", '{"path": "a"}')]),
                _Msg("", [_TC("t2", "read_file", "{bad")]),  # 与上次成功间隔 → 不连2
                _Msg("ANSWER: done", []),
            ]
        )
        eng = _mk_engine(tmp_path, llm)
        result = _run(eng)
        assert result.status == "success"
        assert len(eng._executed) == 1  # 合法那次真的执行了
        rescues = [
            m
            for m in result.messages
            if isinstance(m, dict) and m.get("role") == "user" and "自救提示" in m["content"]
        ]
        assert rescues == []


class TestToolCallCap:
    def test_cap_forces_wrapping(self, tmp_path):
        llm = ScriptedLLM(
            [
                _Msg(
                    "",
                    [
                        _TC("a", "read_file", '{"path": "x"}'),
                        _TC("b", "read_file", '{"path": "y"}'),
                    ],
                ),
                _Msg(
                    "",
                    [
                        _TC("c", "read_file", '{"path": "z"}'),
                        _TC("d", "read_file", '{"path": "w"}'),
                    ],
                ),
                _Msg("ANSWER: wrapped", []),
            ]
        )
        eng = _mk_engine(tmp_path, llm, max_tool_calls=3)
        result = _run(eng)
        assert result.status == "success"
        # 4 次调用都执行（上限检查在批之后），第 2 批结束触发 wrapping
        assert len(eng._executed) == 4
        wraps = [
            m
            for m in result.messages
            if isinstance(m, dict) and m.get("role") == "user" and "【收尾阶段】" in m["content"]
        ]
        assert len(wraps) == 1  # WRAP_MSG 注入一次，走既有收尾通道

    def test_no_cap_by_default(self, tmp_path):
        llm = ScriptedLLM(
            [
                _Msg(
                    "",
                    [
                        _TC("a", "read_file", '{"path": "x"}'),
                        _TC("b", "read_file", '{"path": "y"}'),
                    ],
                ),
                _Msg("ANSWER: done", []),
            ]
        )
        eng = _mk_engine(tmp_path, llm, max_tool_calls=0)
        result = _run(eng)
        assert result.status == "success"
        wraps = [
            m
            for m in result.messages
            if isinstance(m, dict) and m.get("role") == "user" and "【收尾阶段】" in m["content"]
        ]
        assert wraps == []


def test_role_caps_table():
    """主力角色宽（只拦失控）、收敛角色严（20 必须收敛）、未知角色不限。"""
    from secpt.cli.engine_runner import _ROLE_TOOL_CALL_CAPS

    assert _ROLE_TOOL_CALL_CAPS["exploit"] == 100
    assert _ROLE_TOOL_CALL_CAPS["verify"] == 20
    assert _ROLE_TOOL_CALL_CAPS.get("unknown_role", 0) == 0  # 无角色单链不受限
