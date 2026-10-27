"""端到端测试：CLI 交互流程。"""

import pytest

from app.cli.main import _register_default_tools
from harness.tools.registry import ToolRegistry
from runtime.loop import ExecutionLoop
from runtime.agents.base import SubAgent
from runtime.agents.collaboration import Router


class FakeLLM:
    """模拟 LLM，用于测试。"""

    async def chat(self, messages, tools=None):
        return {
            "choices": [{"message": {"content": "mock response"}}],
            "usage": {"total_tokens": 10},
        }


class TestCLIDefaultTools:
    """测试 CLI 默认工具集。"""

    def test_register_default_tools(self):
        """默认工具注册成功（内置工具集 + 无 tools/ 目录时不加载脚本）。"""
        registry = ToolRegistry()
        _register_default_tools(registry)
        names = registry.list_tools()
        # 内置基础工具
        for expected in ("get_time", "calculate", "read_file", "write_file", "http_request"):
            assert expected in names
        # 危险工具被标记，配合 HITL 人工确认
        assert "run_command" in registry.get_dangerous_tools()

    def test_get_time_tool(self):
        """get_time 工具返回时间字符串。"""
        import asyncio

        registry = ToolRegistry()
        _register_default_tools(registry)
        tool = registry.get("get_time")
        result = asyncio.get_event_loop().run_until_complete(tool.execute())
        assert isinstance(result, str)
        assert "-" in result  # 日期格式

    def test_calculate_tool(self):
        """calculate 工具执行计算。"""
        import asyncio

        registry = ToolRegistry()
        _register_default_tools(registry)
        tool = registry.get("calculate")
        result = asyncio.get_event_loop().run_until_complete(tool.execute(expression="2 + 3"))
        assert result == "5"


class TestCLIModes:
    """测试 CLI 两种模式。"""

    @pytest.mark.asyncio
    async def test_single_mode_execution(self):
        """单 Agent 模式可以执行。"""
        llm = FakeLLM()
        tools = ToolRegistry()
        _register_default_tools(tools)

        loop = ExecutionLoop(llm=llm, tools=tools, max_steps=3)
        result = await loop.run_react("测试任务")

        assert result.output is not None
        assert result.steps is not None

    @pytest.mark.asyncio
    async def test_team_mode_execution(self):
        """多 Agent 协作模式可以执行。"""
        llm = FakeLLM()
        tools = ToolRegistry()
        _register_default_tools(tools)

        coder = SubAgent(name="coder", instructions="程序员", llm=llm, tools=tools)
        coordinator = SubAgent(name="coordinator", instructions="协调员", llm=llm)

        router = Router(coordinator=coordinator, sub_agents={"coder": coder})

        result = await router.route("写一个函数")

        assert result.mode == "router"
