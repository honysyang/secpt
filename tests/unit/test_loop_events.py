"""ExecutionLoop 过程事件（on_event）测试：tool_call / tool_result / tool_rejected。

Web 界面的"交互过程"展示依赖这三个事件；此处锁定事件序列与内容契约。
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.tools.registry import Tool, ToolRegistry
from runtime.loop import ExecutionLoop


def _make_echo_tool(calls: list[dict[str, Any]]) -> Tool:
    """构造记录调用的回显工具。"""

    async def execute(**kwargs: Any) -> str:
        calls.append(kwargs)
        return f"echo:{kwargs.get('text', '')}"

    return Tool(
        name="echo",
        description="回显文本",
        func=execute,
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        dangerous=True,  # 危险标记：拒绝路径测试需要 hook 被咨询
    )


def _tool_call_response(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": arguments},
            }],
        }}],
        "usage": {"total_tokens": 10},
    }


def _text_response(text: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text, "tool_calls": []}}],
        "usage": {"total_tokens": 5},
    }


class ScriptedLLM:
    """按脚本逐轮返回响应。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)

    async def chat(self, messages: list[dict], tools=None) -> dict[str, Any]:
        return self._responses.pop(0)


class TestLoopEvents:
    async def test_tool_call_then_tool_result(self):
        calls: list[dict[str, Any]] = []
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool(calls))
        llm = ScriptedLLM([
            _tool_call_response("c1", "echo", '{"text": "hi"}'),
            _text_response("done"),
        ])

        events: list[dict[str, Any]] = []
        loop = ExecutionLoop(llm=llm, tools=tools, on_event=events.append)
        result = await loop.run_react("测试")

        assert result.output == "done"
        assert [e["type"] for e in events] == ["tool_call", "tool_result"]
        assert events[0]["tool"] == "echo"
        assert events[0]["args"] == '{"text": "hi"}'
        assert events[1]["tool"] == "echo"
        assert events[1]["content"] == "echo:hi"

    async def test_reject_emits_tool_rejected_without_result(self):
        calls: list[dict[str, Any]] = []
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool(calls))
        llm = ScriptedLLM([
            _tool_call_response("c1", "echo", '{"text": "hi"}'),
            _text_response("ok"),
        ])

        async def deny(tool_name: str, arguments: str) -> bool:
            return False

        events: list[dict[str, Any]] = []
        loop = ExecutionLoop(llm=llm, tools=tools, hitl_callback=deny, on_event=events.append)
        await loop.run_react("测试")

        # 拒绝路径：有 tool_call 与 tool_rejected，没有 tool_result
        assert [e["type"] for e in events] == ["tool_call", "tool_rejected"]
        assert events[0]["tool"] == "echo"
        assert calls == []  # 拒绝后工具未执行

    async def test_unknown_tool_still_emits_result(self):
        tools = ToolRegistry()
        llm = ScriptedLLM([
            _tool_call_response("c1", "nope", "{}"),
            _text_response("ok"),
        ])
        events: list[dict[str, Any]] = []
        loop = ExecutionLoop(llm=llm, tools=tools, on_event=events.append)
        await loop.run_react("测试")

        assert [e["type"] for e in events] == ["tool_call", "tool_result"]
        assert "不存在" in events[1]["content"]

    async def test_no_callback_is_zero_overhead(self):
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool([]))
        llm = ScriptedLLM([_text_response("plain")])
        loop = ExecutionLoop(llm=llm, tools=tools)  # 不传 on_event
        assert (await loop.run_react("t")).output == "plain"


class TestStreamFallback:
    async def test_chat_stream_none_falls_back_to_chat(self):
        """韧性包装器（ResilientLLM）对不支持流式的底层会把 chat_stream
        显式置为 None——hasattr 判断会把 None 当成能力导致崩溃，
        必须按"非 None"判断并回退非流式 chat。
        """

        class WrappedLLM:
            chat_stream = None  # 模拟 ResilientLLM 包装 AnthropicLLM 后的形态

            async def chat(self, messages, tools=None):
                return _text_response("fallback-ok")

        tokens: list[str] = []
        loop = ExecutionLoop(llm=WrappedLLM(), tools=ToolRegistry(), on_token=tokens.append)
        result = await loop.run_react("t")

        assert result.output == "fallback-ok"
        assert tokens == []  # 非流式路径不产生 token 事件
