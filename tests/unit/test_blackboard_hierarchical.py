"""分层黑板高级特性测试：关键信息、跨领域广播。"""

import asyncio
import time

import pytest

from runtime.blackboard import (
    CapacityExceededError,
    ConflictError,
    HierarchicalBlackboard,
    TTLBlackboardEntry,
)


class TestCriticalEntries:
    """关键信息标记测试。"""

    @pytest.fixture
    def blackboard(self):
        return HierarchicalBlackboard()

    async def test_write_critical_not_expired(self, blackboard):
        """关键信息不会被 TTL 清理。"""
        entry = await blackboard.write_critical(
            key="security_alert",
            value="发现 SQL 注入漏洞",
            agent="security_agent",
            reason="安全漏洞必须保留",
        )

        assert entry.is_critical is True
        assert entry.expire_at == 0  # 永不过期

        # 手动触发清理
        await blackboard._cleanup_expired()

        # 验证仍然存在
        critical = await blackboard.get_critical_entries()
        assert "global:critical_security_alert" in critical

    async def test_pin_existing_entry(self, blackboard):
        """将普通条目标记为关键信息。"""
        # 写入普通条目
        await blackboard.write_global("important_find", "关键发现", ttl=1)

        # 标记为关键
        result = await blackboard.pin_entry("global", "important_find")
        assert result is True

        # 验证已变为关键信息
        entry = blackboard._global["important_find"]
        assert entry.is_critical is True

    async def test_critical_bypass_capacity(self, blackboard):
        """关键信息不受容量限制。"""
        bb = HierarchicalBlackboard(global_max_entries=2)

        # 填满容量
        await bb.write_global("k1", "v1")
        await bb.write_global("k2", "v2")

        # 普通写入应该失败
        with pytest.raises(CapacityExceededError):
            await bb.write_global("k3", "v3")

        # 关键信息可以写入
        entry = await bb.write_global("critical_k3", "v3", critical=True)
        assert entry.is_critical is True

    async def test_get_critical_entries(self, blackboard):
        """获取所有关键信息。"""
        await blackboard.write_critical(
            key="alert1", value="漏洞 1", agent="agent_a", reason="安全"
        )
        await blackboard.write_critical(
            key="alert2", value="漏洞 2", agent="agent_b", reason="性能",
            domains=["network"],
        )
        await blackboard.write_global("normal", "普通信息")  # 非关键

        critical = await blackboard.get_critical_entries()

        assert "global:critical_alert1" in critical
        assert "global:critical_alert2" in critical
        assert "network:alert_alert2" in critical  # 广播的
        assert "global:normal" not in critical


class TestCrossDomainBroadcast:
    """跨领域广播测试。"""

    @pytest.fixture
    def blackboard(self):
        return HierarchicalBlackboard()

    async def test_broadcast_to_single_domain(self, blackboard):
        """广播到单个领域。"""
        await blackboard.broadcast_to_domain(
            source_agent="network_agent",
            key="port_scan_alert",
            value="端口 8080 异常",
            reason="跨领域安全告警",
            target_domain="application",
        )

        # 验证目标领域收到
        value = await blackboard.read_domain("application", "port_scan_alert")
        assert value is not None
        assert value["data"] == "端口 8080 异常"
        assert value["source"] == "network_agent"

    async def test_broadcast_to_multiple_domains(self, blackboard):
        """通过 write_critical 广播到多个领域。"""
        await blackboard.write_critical(
            key="critical_bug",
            value="内存泄漏",
            agent="debugger",
            reason="影响所有模块",
            domains=["frontend", "backend", "database"],
        )

        # 验证所有领域都收到
        for domain in ["frontend", "backend", "database"]:
            value = await blackboard.read_domain(domain, "alert_critical_bug")
            assert value is not None
            assert value["data"] == "内存泄漏"

    async def test_broadcast_critical_not_expired(self, blackboard):
        """广播的关键信息不会被清理。"""
        await blackboard.broadcast_to_domain(
            source_agent="agent_a",
            key="alert",
            value="紧急",
            reason="测试",
            target_domain="test_domain",
            ttl=0.1,  # 短 TTL
        )

        # 等待过期
        await asyncio.sleep(0.2)
        await blackboard._cleanup_expired()

        # 验证仍然存在（因为是关键信息）
        value = await blackboard.read_domain("test_domain", "alert")
        assert value is not None


class TestConflictResolution:
    """冲突解决（乐观锁）测试。"""

    @pytest.fixture
    def blackboard(self):
        return HierarchicalBlackboard()

    async def test_optimistic_lock_success(self, blackboard):
        """乐观锁：版本匹配时写入成功。"""
        entry1 = await blackboard.write_global("counter", 1)

        # 基于版本 1 更新
        entry2 = await blackboard.write_global(
            "counter", 2,
            expected_version=entry1.version,
        )
        assert entry2.version == 2

    async def test_optimistic_lock_conflict(self, blackboard):
        """乐观锁：版本不匹配时抛出冲突。"""
        entry1 = await blackboard.write_global("counter", 1)

        # 其他人更新了
        await blackboard.write_global("counter", 99)

        # 基于过期版本更新应该失败
        with pytest.raises(ConflictError):
            await blackboard.write_global(
                "counter", 2,
                expected_version=entry1.version,  # 过期版本
            )

    async def test_optimistic_lock_missing_key(self, blackboard):
        """乐观锁：键不存在时抛出冲突。"""
        with pytest.raises(ConflictError):
            await blackboard.write_global(
                "nonexistent", 1,
                expected_version=1,
            )


class TestSmartBlackboardAgent:
    """智能黑板 Agent 测试。"""

    @pytest.fixture
    def blackboard(self):
        return HierarchicalBlackboard()

    async def test_agent_writes_critical_for_short_output(self, blackboard):
        """短输出写入全局层。"""
        from runtime.blackboard import SmartBlackboardAgent

        class FakeLLM:
            async def chat(self, messages, tools=None):
                return {
                    "choices": [{"message": {"content": "简短结论"}}],
                    "usage": {"total_tokens": 10},
                }

        agent = SmartBlackboardAgent(
            name="test_agent",
            instructions="测试",
            llm=FakeLLM(),
            blackboard=blackboard,
        )

        result = await agent.run("测试任务")

        # 验证写入了全局层
        conclusion = await blackboard.read_global("test_agent_conclusion")
        assert conclusion is not None

    async def test_agent_writes_domain_for_long_output(self, blackboard):
        """长输出写入领域层。"""
        from runtime.blackboard import SmartBlackboardAgent

        long_output = "详细分析结果" * 100  # 长文本

        class FakeLLM:
            async def chat(self, messages, tools=None):
                return {
                    "choices": [{"message": {"content": long_output}}],
                    "usage": {"total_tokens": 10},
                }

        agent = SmartBlackboardAgent(
            name="analyzer",
            instructions="分析",
            llm=FakeLLM(),
            blackboard=blackboard,
            domain="analysis",
        )

        await agent.run("分析任务")

        # 验证写入了领域层
        result = await blackboard.read_domain("analysis", "analyzer_result")
        assert result is not None
