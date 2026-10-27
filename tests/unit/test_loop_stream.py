"""执行循环测试：tool_calls 消息历史完整性（400 回归）+ 流式输出。"""

from __future__ import annotations

from types import SimpleNamespace
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
    )


class ScriptedLLM:
    """按脚本逐轮返回响应，并记录每轮收到的 messages。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict], tools=None) -> dict[str, Any]:
        self.calls.append([dict(m) for m in messages])
        return self._responses.pop(0)


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


class TestToolCallsHistory:
    """回归：assistant 的 tool_calls 消息必须进入历史（DeepSeek 400）。"""

    @pytest.mark.asyncio
    async def test_tool_message_follows_tool_calls(self):
        tool_calls_log: list[dict[str, Any]] = []
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool(tool_calls_log))

        llm = ScriptedLLM([
            _tool_call_response("call_1", "echo", '{"text": "hi"}'),
            _text_response("done"),
        ])
        loop = ExecutionLoop(llm=llm, tools=tools)
        result = await loop.run_react("测试")

        assert result.output == "done"
        assert len(tool_calls_log) == 1  # 工具确实执行了

        # 第二轮请求的消息序列必须是:
        # [system, user, assistant(tool_calls), tool] —— tool 不能悬空
        second = llm.calls[1]
        assert second[1]["role"] == "user"
        assert second[2]["role"] == "assistant"
        assert second[2]["tool_calls"][0]["id"] == "call_1"
        assert second[3]["role"] == "tool"
        assert second[3]["tool_call_id"] == "call_1"
        assert second[3]["content"] == "echo:hi"

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_kept_in_order(self):
        """并行多工具调用：每个 tool 结果都紧跟同一 assistant 消息。"""
        tool_calls_log: list[dict[str, Any]] = []
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool(tool_calls_log))

        llm = ScriptedLLM([
            _tool_call_response("c1", "echo", '{"text": "a"}'),
            _text_response("ok"),
        ])
        loop = ExecutionLoop(llm=llm, tools=tools)
        await loop.run_react("多工具")

        second = llm.calls[1]
        roles = [m["role"] for m in second]
        assert roles == ["system", "user", "assistant", "tool"]


class TestMaxStepsSummary:
    """达到 max_steps 后强制收敛总结，而不是丢弃执行结果。"""

    @pytest.mark.asyncio
    async def test_summary_called_without_tools(self):
        tool_calls_log: list[dict[str, Any]] = []
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool(tool_calls_log))

        # 2 轮工具调用后耗尽 max_steps=2，第 3 次调用是无工具总结
        llm = ScriptedLLM([
            _tool_call_response("c1", "echo", '{"text": "a"}'),
            _tool_call_response("c2", "echo", '{"text": "b"}'),
            _text_response("扫描结论: 站点正常"),
        ])
        loop = ExecutionLoop(llm=llm, tools=tools, max_steps=2)
        result = await loop.run_react("扫描")

        assert result.output == "扫描结论: 站点正常"
        summary_call = llm.calls[2]
        # 总结请求带完整历史 + 收敛引导，且不提供工具（防止继续调用）
        assert summary_call[-1]["role"] == "user"
        assert "用完工具调用次数" in summary_call[-1]["content"]
        assert "echo:b" in summary_call[-2]["content"]  # 历史观察可见

    @pytest.mark.asyncio
    async def test_summary_empty_content_fallback(self):
        """总结为空时返回明确的降级提示。"""
        calls: list[dict[str, Any]] = []
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool(calls))

        llm = ScriptedLLM([
            _tool_call_response("c1", "echo", '{"text": "a"}'),
            _text_response(""),
        ])
        loop = ExecutionLoop(llm=llm, tools=tools, max_steps=1)

        result = await loop.run_react("任务")
        assert result.output  # 非空：要么总结要么降级提示


class TestStreaming:
    """流式执行：delta 转发 on_token，final 聚合走原逻辑。"""

    @pytest.mark.asyncio
    async def test_stream_text_only(self):
        class StreamLLM:
            async def chat_stream(self, messages, tools=None):
                yield {"type": "delta", "content": "你好"}
                yield {"type": "delta", "content": "，世界"}
                yield {
                    "type": "final",
                    "message": {"role": "assistant", "content": "你好，世界", "tool_calls": []},
                    "usage": {"total_tokens": 7},
                }

        tokens: list[str] = []
        loop = ExecutionLoop(llm=StreamLLM(), tools=ToolRegistry(), on_token=tokens.append)
        result = await loop.run_react("问好")

        assert tokens == ["你好", "，世界"]
        assert result.output == "你好，世界"
        assert result.total_tokens == 7

    @pytest.mark.asyncio
    async def test_stream_with_tool_call(self):
        """流式下工具调用：final 聚合后正常执行，历史包含 tool_calls 消息。"""
        tool_calls_log: list[dict[str, Any]] = []
        tools = ToolRegistry()
        tools.register_tool(_make_echo_tool(tool_calls_log))

        class StreamToolLLM:
            def __init__(self) -> None:
                self.rounds = 0

            async def chat_stream(self, messages, tools=None):
                self.rounds += 1
                if self.rounds == 1:
                    yield {"type": "delta", "content": "让我查一下"}
                    yield {
                        "type": "final",
                        "message": {
                            "role": "assistant", "content": "让我查一下",
                            "tool_calls": [{
                                "id": "s1", "type": "function",
                                "function": {"name": "echo", "arguments": '{"text": "x"}'},
                            }],
                        },
                        "usage": {"total_tokens": 3},
                    }
                else:
                    yield {
                        "type": "final",
                        "message": {"role": "assistant", "content": "完成", "tool_calls": []},
                        "usage": {"total_tokens": 2},
                    }

        llm = StreamToolLLM()
        loop = ExecutionLoop(llm=llm, tools=tools, on_token=lambda t: None)
        result = await loop.run_react("查")

        assert result.output == "完成"
        assert tool_calls_log == [{"text": "x"}]

    @pytest.mark.asyncio
    async def test_fallback_without_chat_stream(self):
        """LLM 无 chat_stream 属性时回退非流式，不报错。"""
        llm = ScriptedLLM([_text_response("plain")])
        loop = ExecutionLoop(llm=llm, tools=ToolRegistry(), on_token=lambda t: None)
        result = await loop.run_react("任务")
        assert result.output == "plain"


class TestAdapterStreamAggregation:
    """OpenAICompatibleLLM.chat_stream 的增量聚合（模拟 DeepSeek 分片协议）。"""

    def _llm_with_chunks(self, chunks: list[Any]) -> Any:
        from adapters.llm.factory import OpenAICompatibleLLM

        class _FakeCompletions:
            def __init__(self, items: list[Any]) -> None:
                self._items = items

            async def create(self, **kwargs: Any):
                self.last_kwargs = kwargs

                async def _gen():
                    for c in self._items:
                        yield c

                return _gen()

        llm = OpenAICompatibleLLM(model="test-model", api_key="test")
        llm.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(chunks)))
        return llm

    @pytest.mark.asyncio
    async def test_aggregates_text_and_tool_calls(self):
        # 模拟: 文本两片 + 工具调用参数跨 chunk 拼接 + usage 收尾帧
        def _chunk(content=None, tool_calls=None):
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))],
                usage=None,
            )

        def _tc(index, id="", name="", arguments=""):
            return SimpleNamespace(
                index=index, id=id or None,
                function=SimpleNamespace(name=name or None, arguments=arguments or None),
            )

        usage_frame = SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=42))

        llm = self._llm_with_chunks([
            _chunk(content="正在"),
            _chunk(content="调用"),
            _chunk(tool_calls=[_tc(0, id="callA", name="echo")]),
            _chunk(tool_calls=[_tc(0, arguments='{"te')]),
            _chunk(tool_calls=[_tc(0, arguments='xt":"hi"}')]),
            _chunk(tool_calls=[_tc(1, id="callB", name="calc")]),
            usage_frame,
        ])

        deltas: list[str] = []
        events: list[dict[str, Any]] = []
        async for event in llm.chat_stream([{"role": "user", "content": "x"}]):
            if event["type"] == "delta":
                deltas.append(event["content"])
            else:
                events.append(event)

        assert deltas == ["正在", "调用"]
        assert len(events) == 1
        final = events[0]
        assert final["message"]["content"] == "正在调用"
        assert final["usage"] == {"total_tokens": 42}

        calls = final["message"]["tool_calls"]
        assert len(calls) == 2
        assert calls[0]["id"] == "callA"
        assert calls[0]["function"] == {"name": "echo", "arguments": '{"text":"hi"}'}
        assert calls[1]["function"]["name"] == "calc"

    @pytest.mark.asyncio
    async def test_stream_params_sent(self):
        llm = self._llm_with_chunks([])
        async for _ in llm.chat_stream([{"role": "user", "content": "x"}]):
            pass
        kwargs = llm.client.chat.completions.last_kwargs
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
