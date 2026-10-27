"""图黑板、冷节点压缩、人机协作测试。"""

import asyncio
import time

import pytest

from runtime.blackboard import (
    EdgeType,
    GraphBlackboard,
    GraphQuery,
    NodeType,
)
from harness.memory.compactor import Compactor, SmartMemoryWithCompaction
from runtime.intervention import (
    HumanInterventionManager,
    InterventionType,
    SteerableAgent,
)


class FakeLLM:
    """Fake LLM，返回预定义响应。"""

    def __init__(self, responses: list[str] | None = None):
        self.responses = responses or ["摘要"]
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.call_count += 1
        response = self.responses[(self.call_count - 1) % len(self.responses)]
        return {
            "choices": [{"message": {"content": response}}],
            "usage": {"total_tokens": 10},
        }


class TestGraphBlackboard:
    """图黑板测试。"""

    def test_add_and_get_node(self):
        """添加和获取节点。"""
        gb = GraphBlackboard()
        node = gb.add_node(
            type=NodeType.ASSET,
            label="API 服务",
            properties={"url": "https://api.example.com"},
        )

        assert node.id is not None
        assert node.type == NodeType.ASSET

        retrieved = gb.get_node(node.id)
        assert retrieved.label == "API 服务"

    def test_add_and_get_edge(self):
        """添加和获取边。"""
        gb = GraphBlackboard()
        node_a = gb.add_node(type=NodeType.FINDING, label="漏洞 A")
        node_b = gb.add_node(type=NodeType.ASSET, label="服务 B")

        edge = gb.add_edge(
            source=node_a.id,
            target=node_b.id,
            type=EdgeType.BELONGS_TO,
        )

        assert edge is not None
        assert edge.source == node_a.id
        assert edge.target == node_b.id

    def test_add_edge_missing_node(self):
        """连接不存在的节点返回 None。"""
        gb = GraphBlackboard()
        result = gb.add_edge("nonexistent", "also_missing", EdgeType.RELATED_TO)
        assert result is None

    def test_query_nodes_by_type(self):
        """按类型查询节点。"""
        gb = GraphBlackboard()
        gb.add_node(type=NodeType.FINDING, label="漏洞 1")
        gb.add_node(type=NodeType.ASSET, label="资产 1")
        gb.add_node(type=NodeType.FINDING, label="漏洞 2")

        query = GraphQuery(node_type=NodeType.FINDING)
        findings = gb.query_nodes(query)

        assert len(findings) == 2
        assert all(n.type == NodeType.FINDING for n in findings)

    def test_query_nodes_by_label(self):
        """按标签模糊查询。"""
        gb = GraphBlackboard()
        gb.add_node(type=NodeType.TASK, label="实现登录功能")
        gb.add_node(type=NodeType.TASK, label="实现注册功能")
        gb.add_node(type=NodeType.TASK, label="修复 bug")

        query = GraphQuery(node_label_contains="实现")
        results = gb.query_nodes(query)

        assert len(results) == 2

    def test_get_neighbors(self):
        """获取邻居节点。"""
        gb = GraphBlackboard()
        center = gb.add_node(type=NodeType.CONCLUSION, label="中心")
        n1 = gb.add_node(type=NodeType.FINDING, label="发现 1")
        n2 = gb.add_node(type=NodeType.FINDING, label="发现 2")
        n3 = gb.add_node(type=NodeType.ASSET, label="资产")

        gb.add_edge(n1.id, center.id, EdgeType.SUPPORTS)
        gb.add_edge(n2.id, center.id, EdgeType.SUPPORTS)
        gb.add_edge(n3.id, center.id, EdgeType.RELATED_TO)

        neighbors = gb.get_neighbors(center.id)
        assert len(neighbors) == 3

        # 只获取特定类型的边
        supports = gb.get_neighbors(center.id, edge_type=EdgeType.SUPPORTS)
        assert len(supports) == 2

    def test_get_subgraph(self):
        """提取子图。"""
        gb = GraphBlackboard()
        center = gb.add_node(type=NodeType.TASK, label="主任务")
        child1 = gb.add_node(type=NodeType.TASK, label="子任务 1")
        child2 = gb.add_node(type=NodeType.TASK, label="子任务 2")
        grandchild = gb.add_node(type=NodeType.TASK, label="孙任务")

        gb.add_edge(center.id, child1.id, EdgeType.DEPENDS_ON)
        gb.add_edge(center.id, child2.id, EdgeType.DEPENDS_ON)
        gb.add_edge(child1.id, grandchild.id, EdgeType.DEPENDS_ON)

        # 深度 1：中心 + 直接子节点
        sub1 = gb.get_subgraph(center.id, depth=1)
        assert len(sub1["nodes"]) == 3

        # 深度 2：包含孙节点
        sub2 = gb.get_subgraph(center.id, depth=2)
        assert len(sub2["nodes"]) == 4

    def test_find_path(self):
        """查找路径。"""
        gb = GraphBlackboard()
        a = gb.add_node(type=NodeType.FINDING, label="A")
        b = gb.add_node(type=NodeType.FINDING, label="B")
        c = gb.add_node(type=NodeType.FINDING, label="C")
        d = gb.add_node(type=NodeType.FINDING, label="D")

        gb.add_edge(a.id, b.id, EdgeType.CAUSES)
        gb.add_edge(b.id, c.id, EdgeType.CAUSES)
        gb.add_edge(c.id, d.id, EdgeType.CAUSES)

        # A → D 的路径
        path = gb.find_path(a.id, d.id)
        assert path is not None
        assert len(path) == 3  # A→B, B→C, C→D

        # 无路径
        gb.add_node(type=NodeType.FINDING, label="孤立节点")
        isolated = gb.add_node(type=NodeType.FINDING, label="孤立")
        no_path = gb.find_path(a.id, isolated.id)
        assert no_path is None

    def test_remove_node_cascades(self):
        """删除节点级联删除边。"""
        gb = GraphBlackboard()
        a = gb.add_node(type=NodeType.ASSET, label="A")
        b = gb.add_node(type=NodeType.FINDING, label="B")
        edge = gb.add_edge(a.id, b.id, EdgeType.HAS_VULNERABILITY)

        assert edge is not None
        assert gb.remove_node(a.id) is True

        # 边也应该被删除
        assert gb.get_edge(edge.id) is None
        assert gb.get_node(b.id) is not None  # B 应该还在

    def test_get_stats(self):
        """获取统计。"""
        gb = GraphBlackboard()
        gb.add_node(type=NodeType.FINDING, label="F1")
        gb.add_node(type=NodeType.FINDING, label="F2")
        gb.add_node(type=NodeType.ASSET, label="A1")

        stats = gb.get_stats()
        assert stats["total_nodes"] == 3
        assert stats["node_types"]["finding"] == 2
        assert stats["node_types"]["asset"] == 1


class TestCompactor:
    """冷节点压缩测试。"""

    async def test_record_access(self):
        """记录访问更新状态。"""
        compactor = Compactor(FakeLLM())
        compactor.record_access("node1")

        stamp = compactor._stamps["node1"]
        assert stamp.access_count == 1
        assert stamp.is_cold is False

    async def test_mark_cold_nodes(self):
        """标记冷节点。"""
        compactor = Compactor(FakeLLM(), cold_threshold_seconds=0.1)

        compactor.record_access("hot_node")
        compactor.record_access("cold_node")
        compactor._stamps["cold_node"].last_access = time.time() - 1000

        cold = compactor.mark_cold_nodes()
        assert "cold_node" in cold
        assert "hot_node" not in cold

    async def test_compact_node(self):
        """压缩节点生成摘要。"""
        llm = FakeLLM(["这是压缩后的摘要"])
        compactor = Compactor(llm, min_access_count=1)

        compactor.record_access("node1")
        digest = await compactor.compact_node("node1", "原始长内容" * 100)

        assert digest == "这是压缩后的摘要"
        assert compactor.is_compacted("node1")

    async def test_compact_node_low_access_skipped(self):
        """访问次数太少不压缩。"""
        compactor = Compactor(FakeLLM(), min_access_count=5)
        compactor.record_access("node1")  # 只访问 1 次

        digest = await compactor.compact_node("node1", "内容")
        assert digest is None

    async def test_get_node_content_compacted(self):
        """获取压缩后的内容返回摘要。"""
        llm = FakeLLM(["摘要内容"])
        compactor = Compactor(llm, min_access_count=1)
        compactor.record_access("node1")

        await compactor.compact_node("node1", "原始内容")

        content = compactor.get_node_content("node1", "原始内容")
        assert content == "摘要内容"

    async def test_restore_node(self):
        """恢复压缩的节点。"""
        llm = FakeLLM(["摘要"])
        compactor = Compactor(llm, min_access_count=1)
        compactor.record_access("node1")
        await compactor.compact_node("node1", "内容")

        assert compactor.is_compacted("node1")

        result = compactor.restore_node("node1")
        assert result is True
        assert not compactor.is_compacted("node1")

    async def test_get_stats(self):
        """获取统计。"""
        compactor = Compactor(FakeLLM())
        compactor.record_access("n1")
        compactor.record_access("n2")

        stats = compactor.get_stats()
        assert stats.total_nodes == 2
        assert stats.cold_nodes == 0
        assert stats.compacted_nodes == 0


class TestSmartMemoryWithCompaction:
    """带压缩的智能记忆测试。"""

    async def test_remember_and_recall(self):
        """存储和回忆。"""
        memory = SmartMemoryWithCompaction(FakeLLM(), max_active_memories=2)
        await memory.remember("key1", "内容 1")

        result = await memory.recall("key1")
        assert result == "内容 1"

    async def test_auto_compact_oldest(self):
        """容量满时自动压缩最旧的。"""
        llm = FakeLLM(["摘要"])
        memory = SmartMemoryWithCompaction(llm, max_active_memories=2)

        await memory.remember("k1", "内容 1")
        await memory.remember("k2", "内容 2")

        # 访问 k1，使 k2 成为最旧
        await memory.recall("k1")
        await asyncio.sleep(0.1)

        # 写入 k3，触发压缩
        await memory.remember("k3", "内容 3")

        # k2 应该被压缩
        assert "k2" in memory._compacted_memories
        assert "k3" in memory._memories

    async def test_recall_compacted_returns_digest(self):
        """回忆压缩的记忆返回摘要。"""
        llm = FakeLLM(["压缩摘要"])
        memory = SmartMemoryWithCompaction(llm, max_active_memories=1)

        await memory.remember("k1", "长内容")
        await memory.recall("k1")  # 访问
        await asyncio.sleep(0.1)

        await memory.remember("k2", "新内容")  # 触发 k1 压缩

        result = await memory.recall("k1")
        assert result == "压缩摘要"


class TestHumanIntervention:
    """人机协作测试。"""

    async def test_steer_agent(self):
        """干预 Agent。"""
        manager = HumanInterventionManager()

        received_messages = []
        manager.register_agent("agent1", lambda msg: received_messages.append(msg))

        await manager.steer("agent1", "改变方向，先处理高优先级任务")

        assert len(received_messages) == 1
        assert "改变方向" in received_messages[0]

    async def test_pause_and_resume(self):
        """暂停和恢复。"""
        manager = HumanInterventionManager()

        states = []
        def callback(msg):
            states.append(msg)

        manager.register_agent("agent1", callback)

        await manager.pause_agent("agent1", "等待用户输入")
        await manager.resume_agent("agent1")

        assert states[0].startswith("PAUSE")
        assert states[1] == "RESUME"

    async def test_cancel_agent(self):
        """取消 Agent。"""
        manager = HumanInterventionManager()

        messages = []
        manager.register_agent("agent1", lambda msg: messages.append(msg))

        await manager.cancel_agent("agent1", "任务已废弃")

        assert "CANCEL" in messages[0]

    async def test_inject_context(self):
        """注入上下文。"""
        manager = HumanInterventionManager()

        contexts = []
        manager.register_agent("agent1", lambda msg: contexts.append(msg))

        await manager.inject_context("agent1", "补充信息：用户偏好简洁回答")

        assert "用户偏好" in contexts[0]

    async def test_side_question(self):
        """边问机制。"""
        manager = HumanInterventionManager()

        received_questions = []
        manager.on_question(lambda q: received_questions.append(q))

        q = await manager.ask(
            agent="agent1",
            question="需要确认权限范围",
            context="正在执行删除操作",
        )

        assert len(received_questions) == 1
        assert received_questions[0].question == "需要确认权限范围"
        assert received_questions[0].answered is False

    async def test_answer_question(self):
        """回答问题。"""
        manager = HumanInterventionManager()
        q = await manager.ask(agent="agent1", question="确认执行？")

        result = await manager.answer(q.id, "确认执行")
        assert result is not None
        assert result.answered is True
        assert result.answer == "确认执行"

    async def test_wait_for_answer(self):
        """等待回答（模拟异步）。"""
        manager = HumanInterventionManager()
        q = await manager.ask(agent="agent1", question="选择方案？")

        # 延迟回答
        async def delayed_answer():
            await asyncio.sleep(0.2)
            await manager.answer(q.id, "选择方案 B")

        asyncio.create_task(delayed_answer())

        answer = await manager.wait_for_answer(q.id, timeout=1.0)
        assert answer == "选择方案 B"


class TestSteerableAgent:
    """可干预 Agent 测试。"""

    async def test_run_with_injection(self):
        """运行时注入上下文。"""
        manager = HumanInterventionManager()
        agent = SteerableAgent(
            agent_id="test",
            instructions="你是助手",
            llm=FakeLLM(),
            intervention_manager=manager,
        )

        # 先注入上下文
        await manager.inject_context("test", "用户偏好简洁回答")

        # 执行任务
        result = await agent.run("介绍 Python")

        # FakeLLM 会记录收到的 prompt，这里只验证不报错
        assert result is not None

    async def test_pause_and_resume(self):
        """暂停和恢复执行。"""
        manager = HumanInterventionManager()
        agent = SteerableAgent(
            agent_id="test",
            instructions="你是助手",
            llm=FakeLLM(),
            intervention_manager=manager,
        )

        # 暂停
        agent.pause()
        assert agent._paused is True

        # 恢复
        agent.resume()
        assert agent._paused is False

    async def test_cancel(self):
        """取消执行。"""
        manager = HumanInterventionManager()
        agent = SteerableAgent(
            agent_id="test",
            instructions="你是助手",
            llm=FakeLLM(),
            intervention_manager=manager,
        )

        agent.cancel()
        assert agent._cancelled is True
