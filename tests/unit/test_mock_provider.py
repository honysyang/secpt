"""MockLLM 提供商测试：无需 API Key 的离线演示链路。

Mock 脚本行为：
- 带 tools 且历史无工具结果 → 调用 run_command（dangerous，触发审批演示）
- 历史已有工具结果 → 基于结果流式收尾
- 不带 tools → 纯文本（路由/总结场景）
"""

from __future__ import annotations

from typing import Any

import pytest

from adapters.llm.factory import MockLLM, build_llm
from harness.tools.builtin import register_builtin
from harness.tools.registry import ToolRegistry
from runtime.loop import ExecutionLoop


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    register_builtin(registry)
    return registry


class TestMockProvider:
    def test_build_llm_mock_needs_no_key(self):
        assert isinstance(build_llm("mock"), MockLLM)

    async def test_full_react_loop_with_approval(self):
        """mock 脚本：先发起危险工具调用（触发审批），批准后基于结果收尾。"""
        approvals: list[tuple[str, str]] = []

        async def auto_approve(tool_name: str, arguments: str) -> bool:
            approvals.append((tool_name, arguments))
            return True

        loop = ExecutionLoop(
            llm=MockLLM(), tools=_tools(), max_steps=5, hitl_callback=auto_approve
        )
        result = await loop.run_react("演示任务")

        assert approvals and approvals[0][0] == "run_command"
        assert "mock-web-demo" in result.output  # 收尾回答引用了工具结果

    async def test_rejected_tool_still_completes(self):
        async def deny(tool_name: str, arguments: str) -> bool:
            return False

        loop = ExecutionLoop(
            llm=MockLLM(), tools=_tools(), max_steps=5, hitl_callback=deny
        )
        result = await loop.run_react("演示任务")
        assert "拒绝" in result.output  # mock 基于"已拒绝"观察收尾

    async def test_plain_chat_without_tools_is_text(self):
        """不带 tools（路由决策 / run_simple）→ 纯文本，不误触发工具。"""
        response = await MockLLM().chat([{"role": "user", "content": "路由测试"}])
        message = response["choices"][0]["message"]
        assert message["tool_calls"] == []
        assert message["content"]

    async def test_stream_emits_deltas_and_final(self):
        llm = MockLLM()
        deltas: list[str] = []
        finals: list[dict[str, Any]] = []
        async for event in llm.chat_stream(
            [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]
        ):
            if event["type"] == "delta":
                deltas.append(event["content"])
            else:
                finals.append(event)

        assert deltas  # 有流式增量
        assert len(finals) == 1  # 收尾必发 final
        assert "tool_calls" in finals[0]["message"]
