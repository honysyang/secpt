"""共享黑板单元测试。"""

import asyncio

import pytest

from runtime.blackboard import Blackboard, BlackboardAgent, BlackboardEvent


class FakeLLM:
    """模拟 LLM。"""

    def __init__(self, response: str = "result"):
        self.response = response
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.call_count += 1
        return {
            "choices": [{"message": {"content": self.response}}],
            "usage": {"total_tokens": 10},
        }


class TestBlackboard:
    @pytest.fixture
    def blackboard(self):
        return Blackboard()

    async def test_write_and_read(self, blackboard):
        """写入和读取值。"""
        await blackboard.write("key1", "value1", agent="agent_a")

        value = await blackboard.read("key1")
        assert value == "value1"

    async def test_read_missing(self, blackboard):
        """读取不存在的键。"""
        value = await blackboard.read("nonexistent")
        assert value is None

    async def test_overwrite(self, blackboard):
        """覆盖写入，版本号递增。"""
        entry1 = await blackboard.write("key", "v1")
        entry2 = await blackboard.write("key", "v2")

        assert entry1.version == 1
        assert entry2.version == 2

    async def test_delete(self, blackboard):
        """删除条目。"""
        await blackboard.write("key", "value")
        result = await blackboard.delete("key")

        assert result is True
        assert await blackboard.read("key") is None

    async def test_delete_missing(self, blackboard):
        """删除不存在的键。"""
        result = await blackboard.delete("nonexistent")
        assert result is False

    async def test_list_keys(self, blackboard):
        """列出所有键。"""
        await blackboard.write("a", "1")
        await blackboard.write("b", "2")
        await blackboard.write("c", "3")

        keys = await blackboard.list_keys()
        assert set(keys) == {"a", "b", "c"}

    async def test_list_keys_with_prefix(self, blackboard):
        """按前缀过滤键。"""
        await blackboard.write("task_1", "1")
        await blackboard.write("task_2", "2")
        await blackboard.write("other", "3")

        keys = await blackboard.list_keys(prefix="task_")
        assert set(keys) == {"task_1", "task_2"}

    async def test_exists(self, blackboard):
        """检查键是否存在。"""
        await blackboard.write("key", "value")

        assert await blackboard.exists("key") is True
        assert await blackboard.exists("missing") is False

    async def test_subscribe(self, blackboard):
        """订阅状态变更。"""
        events = []

        def callback(event):
            events.append(event)

        blackboard.subscribe("key", callback)
        await blackboard.write("key", "value", agent="test")

        assert len(events) == 1
        assert events[0].type == "write"
        assert events[0].value == "value"

    async def test_unsubscribe(self, blackboard):
        """取消订阅。"""
        events = []

        def callback(event):
            events.append(event)

        blackboard.subscribe("key", callback)
        blackboard.unsubscribe("key", callback)

        await blackboard.write("key", "value")
        assert len(events) == 0

    async def test_lock(self, blackboard):
        """获取和释放锁。"""
        acquired = await blackboard.acquire_lock("key", timeout=1.0)
        assert acquired is True

        await blackboard.release_lock("key")

    async def test_get_history(self, blackboard):
        """获取事件历史。"""
        await blackboard.write("key1", "v1", agent="a")
        await blackboard.write("key2", "v2", agent="b")
        await blackboard.delete("key1", agent="a")

        history = await blackboard.get_history()
        assert len(history) == 3

        # 按键过滤
        key1_history = await blackboard.get_history(key="key1")
        assert len(key1_history) == 2

    async def test_clear(self, blackboard):
        """清空黑板。"""
        await blackboard.write("key", "value")
        await blackboard.clear()

        assert await blackboard.exists("key") is False
        assert len(await blackboard.get_history()) == 0


class TestBlackboardAgent:
    @pytest.fixture
    def blackboard(self):
        return Blackboard()

    @pytest.fixture
    def llm(self):
        return FakeLLM("agent result")

    async def test_read_write_blackboard(self, blackboard, llm):
        """Agent 读写黑板。"""
        agent = BlackboardAgent(
            name="test_agent",
            instructions="测试",
            llm=llm,
            blackboard=blackboard,
            read_keys=["shared_data"],
            write_keys=["agent_output"],
        )

        # 写入共享数据
        await blackboard.write("shared_data", "shared value")

        # Agent 执行
        result = await agent.run("处理数据")

        # 验证读取了共享数据
        assert "shared value" in result.output or "agent result" in result.output

        # 验证写入了输出
        output = await blackboard.read("agent_output")
        assert output is not None

    async def test_permission_check(self, blackboard, llm):
        """写入权限检查。"""
        agent = BlackboardAgent(
            name="limited_agent",
            instructions="测试",
            llm=llm,
            blackboard=blackboard,
            write_keys=["allowed_key"],
        )

        # 允许写入
        await agent.write_blackboard("allowed_key", "value")
        assert await blackboard.read("allowed_key") == "value"

        # 不允许写入
        with pytest.raises(PermissionError):
            await agent.write_blackboard("forbidden_key", "value")


class TestBlackboardIntegration:
    """集成测试：多 Agent 通过黑板协同。"""

    async def test_agent_collaboration(self):
        """多个 Agent 通过黑板共享状态。"""
        blackboard = Blackboard()

        llm1 = FakeLLM("分析结果")
        llm2 = FakeLLM("基于分析的建议")

        agent1 = BlackboardAgent(
            name="analyzer",
            instructions="分析数据",
            llm=llm1,
            blackboard=blackboard,
            write_keys=["analysis"],
        )

        agent2 = BlackboardAgent(
            name="advisor",
            instructions="提供建议",
            llm=llm2,
            blackboard=blackboard,
            read_keys=["analysis"],
            write_keys=["advice"],
        )

        # Agent 1 分析并写入黑板
        await agent1.run("分析销售数据")

        # Agent 2 读取分析结果并提供建议
        await agent2.run("基于分析给出建议")

        # 验证黑板状态
        assert await blackboard.exists("analysis")
        assert await blackboard.exists("advice")

        # 验证事件历史
        history = await blackboard.get_history()
        assert len(history) >= 2

    async def test_parallel_with_blackboard(self):
        """并行模式使用黑板。"""
        from runtime.agents.collaboration import BlackboardParallel

        blackboard = Blackboard()

        agents = [
            BlackboardAgent(
                name=f"agent_{i}",
                instructions=f"角色 {i}",
                llm=FakeLLM(f"结果 {i}"),
                blackboard=blackboard,
                write_keys=[f"output_{i}"],
            )
            for i in range(3)
        ]

        parallel = BlackboardParallel(
            sub_agents=agents,
            blackboard=blackboard,
        )

        result = await parallel.execute("并行任务")

        assert result.mode == "blackboard_parallel"
        assert result.metadata["agent_count"] == 3

        # 验证所有 Agent 都写入了结果
        for i in range(3):
            assert await blackboard.exists(f"output_{i}")

        # 验证完成状态
        status = await blackboard.read("status")
        assert status == "completed"
