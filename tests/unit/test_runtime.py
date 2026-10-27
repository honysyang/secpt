"""运行时模块测试。"""

import asyncio

import pytest

from harness.planner.base import Plan, PlanStep
from harness.tools.registry import ToolRegistry
from runtime.events import EventBus
from runtime.executor.engine import Executor
from runtime.loop import ExecutionLoop
from runtime.state import State, StateMachine


class FakeLLM:
    """模拟 LLM 客户端。"""

    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.call_count = 0

    async def chat(self, messages, tools=None):
        response = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return response


class TestExecutionLoop:
    @pytest.fixture
    def tools(self):
        registry = ToolRegistry()

        async def add(a: int, b: int) -> int:
            return a + b

        registry.register("add", add)
        return registry

    @pytest.fixture
    def llm_no_tool(self):
        return FakeLLM([
            {
                "choices": [{"message": {"content": "直接回答"}}],
                "usage": {"total_tokens": 10},
            }
        ])

    @pytest.fixture
    def llm_with_tool(self):
        return FakeLLM([
            # 第一次：请求调用工具
            {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                        }],
                    }
                }],
                "usage": {"total_tokens": 20},
            },
            # 第二次：最终回答
            {
                "choices": [{"message": {"content": "结果是 5"}}],
                "usage": {"total_tokens": 15},
            },
        ])

    async def test_react_no_tool(self, llm_no_tool, tools):
        loop = ExecutionLoop(llm_no_tool, tools)
        result = await loop.run_react("1+1=?")

        assert result.output == "直接回答"
        assert result.total_tokens == 10
        assert len(result.steps) == 1
        assert result.steps[0]["type"] == "thought"

    async def test_react_with_tool(self, llm_with_tool, tools):
        loop = ExecutionLoop(llm_with_tool, tools)
        result = await loop.run_react("2+3=?")

        assert result.output == "结果是 5"
        assert result.total_tokens == 35
        assert len(result.steps) == 4  # thought + action + observation + final thought

    async def test_react_max_steps(self, tools):
        # 永远返回工具调用的 LLM
        infinite_llm = FakeLLM([
            {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": f"call_{i}",
                            "function": {"name": "add", "arguments": '{"a": 1, "b": 1}'},
                        }],
                    }
                }],
            }
            for i in range(20)
        ])
        loop = ExecutionLoop(infinite_llm, tools, max_steps=3)
        result = await loop.run_react("无限循环")

        assert "最大步数" in result.output
        assert len(result.steps) <= 9  # 3 步 * 3 条记录


class TestExecutor:
    async def test_execute_without_loop(self):
        executor = Executor()
        plan = Plan(goal="测试", steps=[PlanStep(action="步骤1")])
        result = await executor.execute(plan)

        assert result["status"] == "placeholder"
        assert result["goal"] == "测试"

    async def test_execute_react(self):
        llm = FakeLLM([
            {
                "choices": [{"message": {"content": "完成"}}],
                "usage": {"total_tokens": 5},
            }
        ])
        tools = ToolRegistry()
        loop = ExecutionLoop(llm, tools)
        executor = Executor(loop)

        result = await executor.execute_react("任务")
        assert result["status"] == "completed"
        assert result["output"] == "完成"


class TestStateMachine:
    def test_valid_transition(self):
        sm = StateMachine()
        assert sm.state == State.IDLE

        sm.transition(State.PLANNING)
        assert sm.state == State.PLANNING

        sm.transition(State.EXECUTING)
        assert sm.state == State.EXECUTING

        sm.transition(State.COMPLETED)
        assert sm.state == State.COMPLETED

    def test_invalid_transition(self):
        sm = StateMachine()
        with pytest.raises(ValueError, match="非法状态转换"):
            sm.transition(State.COMPLETED)  # IDLE 不能直接到 COMPLETED

    def test_can_transition(self):
        sm = StateMachine()
        assert sm.can_transition(State.PLANNING)
        assert sm.can_transition(State.EXECUTING)
        assert not sm.can_transition(State.COMPLETED)

    def test_reset(self):
        sm = StateMachine()
        sm.transition(State.PLANNING)
        sm.reset()
        assert sm.state == State.IDLE
        assert sm.history == []

    def test_history(self):
        sm = StateMachine()
        sm.transition(State.PLANNING)
        sm.transition(State.EXECUTING)
        sm.transition(State.COMPLETED)

        assert sm.history == ["IDLE", "PLANNING", "EXECUTING"]


class TestEventBus:
    async def test_on_and_emit(self):
        bus = EventBus()
        received = []

        def listener(event):
            received.append(event.data["msg"])

        bus.on("test", listener)
        await bus.emit("test", {"msg": "hello"})

        assert received == ["hello"]

    async def test_once(self):
        bus = EventBus()
        count = 0

        def listener(event):
            nonlocal count
            count += 1

        bus.once("test", listener)
        await bus.emit("test")
        await bus.emit("test")

        assert count == 1  # 只触发一次

    async def test_off(self):
        bus = EventBus()
        received = []

        def listener(event):
            received.append(1)

        bus.on("test", listener)
        bus.off("test", listener)
        await bus.emit("test")

        assert received == []

    async def test_history(self):
        bus = EventBus()
        await bus.emit("a", {"x": 1})
        await bus.emit("b", {"y": 2})
        await bus.emit("a", {"z": 3})

        assert len(bus.get_history()) == 3
        assert len(bus.get_history("a")) == 2

    async def test_async_listener(self):
        bus = EventBus()
        received = []

        async def async_listener(event):
            await asyncio.sleep(0.01)
            received.append(event.data)

        bus.on("async", async_listener)
        await bus.emit("async", {"ok": True})

        assert received == [{"ok": True}]
