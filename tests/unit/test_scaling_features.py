"""流式输出、持久化、插件市场、分布式执行测试。"""

import asyncio
import json
import time

import pytest

from runtime.streaming import (
    EventStream,
    SSEStream,
    StreamEventType,
    StreamingExecutionLoop,
)
from harness.persistence import CheckpointManager, Snapshot, SnapshotStore
from plugins.marketplace import (
    AgentPlugin,
    ExampleToolPlugin,
    PluginMarketplace,
    PluginMetadata,
    SkillPlugin,
    ToolPlugin,
)
from runtime.distributed import (
    ClusterManager,
    DistributedExecutor,
    DistributedTask,
    NodeInfo,
    NodeRegistry,
    NodeStatus,
    TaskQueue,
)


class FakeLLM:
    """Fake LLM。"""

    def __init__(self, responses: list[str] | None = None):
        self.responses = responses or ["Hello World"]
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.call_count += 1
        response = self.responses[(self.call_count - 1) % len(self.responses)]
        return {
            "choices": [{"message": {"content": response}}],
            "usage": {"total_tokens": 10},
        }


class TestStreaming:
    """流式输出测试。"""

    async def test_event_stream_publish_subscribe(self):
        """发布订阅事件。"""
        stream = EventStream()

        events_received = []

        async def subscriber():
            async for event in stream.subscribe():
                events_received.append(event)
                if event.type == StreamEventType.END:
                    break

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0.1)

        await stream.publish(StreamEventType.TOKEN, "Hello")
        await stream.publish(StreamEventType.TOKEN, " World")
        await stream.publish(StreamEventType.END, None)

        await task

        assert len(events_received) == 3
        assert events_received[0].data == "Hello"

    async def test_sse_stream_format(self):
        """SSE 格式输出。"""
        sse = SSEStream()

        chunks = []

        async def collect():
            async for chunk in sse.stream():
                chunks.append(chunk)
                if '"type": "end"' in chunk:
                    break

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)

        await sse.publish_token("Test")
        await sse.finish()

        await task

        assert len(chunks) == 2
        assert chunks[0].startswith("data: ")
        assert '"type": "token"' in chunks[0]

    async def test_streaming_execution_loop(self):
        """流式执行循环。"""
        llm = FakeLLM(["这是最终回答"])
        loop = StreamingExecutionLoop(llm)

        # 收集 SSE 事件
        events = []

        async def collect():
            async for chunk in loop.sse.stream():
                events.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)

        result = await loop.run_react_streaming("测试任务", max_steps=1)
        await asyncio.sleep(0.3)

        assert result == "这是最终回答"
        # 验证有 start 和 end 事件（通过检查序列）
        assert len(events) >= 2


class TestPersistence:
    """持久化测试。"""

    @pytest.fixture
    def temp_storage(self, tmp_path):
        return tmp_path / "snapshots"

    async def test_save_and_load_snapshot(self, temp_storage):
        """保存和加载快照。"""
        store = SnapshotStore(temp_storage)

        # 创建黑板 Mock
        class MockBB:
            def __init__(self):
                self._global = {"key1": type("E", (), {"value": "v1", "agent": "a"})()}

        bb = MockBB()

        snapshot_id = await store.save(bb, metadata={"test": True})
        assert snapshot_id is not None

        loaded = await store.load(snapshot_id)
        assert loaded is not None
        assert loaded.metadata["test"] is True

    async def test_list_snapshots(self, temp_storage):
        """列出快照。"""
        store = SnapshotStore(temp_storage)

        class MockBB:
            _global = {}

        await store.save(MockBB())
        await asyncio.sleep(0.1)  # 确保时间戳不同
        await store.save(MockBB())

        snapshots = await store.list_snapshots()
        assert len(snapshots) == 2

    async def test_delete_snapshot(self, temp_storage):
        """删除快照。"""
        store = SnapshotStore(temp_storage)

        class MockBB:
            _global = {}

        snapshot_id = await store.save(MockBB())
        result = await store.delete_snapshot(snapshot_id)
        assert result is True

        loaded = await store.load(snapshot_id)
        assert loaded is None


class TestPluginMarketplace:
    """插件市场测试。"""

    def test_discover_empty(self, tmp_path):
        """空目录发现。"""
        market = PluginMarketplace(tmp_path)
        plugins = market.discover()
        assert plugins == []

    async def test_load_example_plugin(self, tmp_path):
        """加载示例插件。"""
        # 创建插件目录
        plugin_dir = tmp_path / "example"
        plugin_dir.mkdir()

        # 创建 plugin.json
        import json
        with open(plugin_dir / "plugin.json", "w") as f:
            json.dump({
                "name": "example",
                "version": "1.0.0",
                "description": "Test plugin",
                "entry_point": "example",
            }, f)

        # 创建 __init__.py
        with open(plugin_dir / "__init__.py", "w") as f:
            f.write("""
from plugins.marketplace import ToolPlugin

class ExamplePlugin(ToolPlugin):
    @property
    def name(self): return "example"
    @property
    def version(self): return "1.0.0"
    async def initialize(self, context): pass
    def get_tools(self): return [{"name": "test_tool", "func": lambda: "ok", "description": "test"}]
""")

        market = PluginMarketplace(tmp_path)
        plugins = market.discover()
        assert len(plugins) == 1
        assert plugins[0].name == "example"

    async def test_example_tool_plugin(self):
        """示例工具插件。"""
        plugin = ExampleToolPlugin()
        tools = plugin.get_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "example_tool"

    async def test_unload_plugin(self):
        """卸载插件。"""
        market = PluginMarketplace("/tmp/test_plugins")
        plugin = ExampleToolPlugin()

        # 手动加载
        market._loaded["test"] = type("LP", (), {
            "metadata": PluginMetadata(name="test", version="1.0", description=""),
            "instance": plugin,
        })()

        result = await market.unload("test")
        assert result is True
        assert "test" not in market._loaded


class TestDistributed:
    """分布式执行测试。"""

    async def test_task_queue_submit_and_get(self):
        """提交和获取任务。"""
        queue = TaskQueue()
        task = DistributedTask(
            id="t1",
            task_type="coding",
            payload={"code": "print('hello')"},
        )
        await queue.submit(task)

        retrieved = await queue.get_task(["coding"])
        assert retrieved is not None
        assert retrieved.id == "t1"

    async def test_task_queue_no_match(self):
        """无匹配能力返回 None。"""
        queue = TaskQueue()
        task = DistributedTask(id="t1", task_type="coding", payload={})
        await queue.submit(task)

        result = await queue.get_task(["research"])  # 不同能力
        assert result is None

    async def test_task_queue_priority(self):
        """优先级排序。"""
        queue = TaskQueue()

        await queue.submit(DistributedTask(id="low", task_type="t", payload={}, priority=1))
        await queue.submit(DistributedTask(id="high", task_type="t", payload={}, priority=10))

        first = await queue.get_task(["t"])
        assert first.id == "high"

    async def test_node_registry(self):
        """节点注册表。"""
        registry = NodeRegistry()

        node = NodeInfo(
            node_id="node1",
            address="localhost:8080",
            capabilities=["coding", "testing"],
        )
        await registry.register(node)

        nodes = await registry.get_available_nodes("coding")
        assert len(nodes) == 1
        assert nodes[0].node_id == "node1"

    async def test_node_heartbeat_timeout(self):
        """节点心跳超时。"""
        registry = NodeRegistry()

        node = NodeInfo(node_id="node1", address="localhost")
        node.last_heartbeat = time.time() - 100  # 100 秒前
        await registry.register(node)

        offline = await registry.check_timeouts(timeout_seconds=30)
        assert "node1" in offline

    async def test_distributed_executor(self):
        """分布式执行器。"""
        queue = TaskQueue()
        registry = NodeRegistry()

        executor = DistributedExecutor(
            node_id="worker1",
            capabilities=["coding"],
            task_queue=queue,
            node_registry=registry,
        )

        # 提交任务
        await queue.submit(DistributedTask(
            id="task1",
            task_type="coding",
            payload={"lang": "python"},
        ))

        # 处理函数
        async def handler(task):
            return f"Completed: {task.payload['lang']}"

        await executor.start(handler)
        await asyncio.sleep(0.5)
        await executor.stop()

        # 验证结果
        async with queue._lock:
            result = queue._tasks["task1"].result
        assert "Completed" in result

    async def test_cluster_manager(self):
        """集群管理器。"""
        cluster = ClusterManager()

        # 创建执行器
        executor = cluster.create_executor(
            node_id="node1",
            capabilities=["coding"],
        )

        # 提交任务
        task_id = await cluster.submit_task("coding", {"code": "test"})

        # 处理并等待结果
        async def handler(task):
            return "done"

        await executor.start(handler)

        try:
            result = await asyncio.wait_for(
                cluster.get_task_result(task_id, timeout=5),
                timeout=10,
            )
            assert result == "done"
        finally:
            await executor.stop()
