"""分布式执行：多节点 Agent 协作。

支持：
- 任务队列（Redis / RabbitMQ 风格）
- 节点发现和心跳
- 负载均衡
- 故障转移
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeStatus(StrEnum):
    """节点状态。"""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    DEGRADED = "degraded"


@dataclass
class NodeInfo:
    """节点信息。"""

    node_id: str
    address: str  # 地址（IP:Port）
    status: NodeStatus = NodeStatus.ONLINE
    capabilities: list[str] = field(default_factory=list)  # 能力标签
    current_load: int = 0  # 当前负载
    max_load: int = 10  # 最大并发
    last_heartbeat: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DistributedTask:
    """分布式任务。"""

    id: str
    task_type: str  # 任务类型（对应能力标签）
    payload: dict[str, Any]
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    assigned_node: str | None = None
    status: str = "pending"
    result: Any | None = None
    error: str | None = None


class TaskQueue:
    """分布式任务队列（内存实现，可替换为 Redis）。"""

    def __init__(self) -> None:
        """初始化任务队列。"""
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._tasks: dict[str, DistributedTask] = {}
        self._lock = asyncio.Lock()

    async def submit(self, task: DistributedTask) -> None:
        """提交任务。

        Args:
            task: 分布式任务
        """
        async with self._lock:
            self._tasks[task.id] = task
            await self._queue.put((-task.priority, task.id))

    async def get_task(self, capabilities: list[str]) -> DistributedTask | None:
        """获取匹配能力的任务。

        Args:
            capabilities: 节点能力标签

        Returns:
            匹配的任务，无匹配返回 None
        """
        # 简单实现：遍历找匹配（实际可用更高效的索引）
        candidates = []

        async with self._lock:
            for task in self._tasks.values():
                if task.status == "pending" and task.task_type in capabilities:
                    candidates.append(task)

        if not candidates:
            return None

        # 按优先级排序
        candidates.sort(key=lambda t: -t.priority)
        selected = candidates[0]

        async with self._lock:
            selected.status = "assigned"
            selected.assigned_node = "pending"  # 待节点确认

        return selected

    async def complete(self, task_id: str, result: Any) -> None:
        """完成任务。"""
        async with self._lock:
            if task := self._tasks.get(task_id):
                task.status = "completed"
                task.result = result

    async def fail(self, task_id: str, error: str) -> None:
        """标记任务失败。"""
        async with self._lock:
            if task := self._tasks.get(task_id):
                task.status = "failed"
                task.error = error


class NodeRegistry:
    """节点注册表：发现和跟踪节点。"""

    def __init__(self) -> None:
        """初始化注册表。"""
        self._nodes: dict[str, NodeInfo] = {}
        self._lock = asyncio.Lock()

    async def register(self, node: NodeInfo) -> None:
        """注册节点。"""
        async with self._lock:
            self._nodes[node.node_id] = node

    async def unregister(self, node_id: str) -> None:
        """注销节点。"""
        async with self._lock:
            if node_id in self._nodes:
                del self._nodes[node_id]

    async def heartbeat(self, node_id: str) -> bool:
        """节点心跳。"""
        async with self._lock:
            if node := self._nodes.get(node_id):
                node.last_heartbeat = time.time()
                return True
            return False

    async def get_available_nodes(self, capability: str | None = None) -> list[NodeInfo]:
        """获取可用节点。"""
        async with self._lock:
            nodes = []
            for node in self._nodes.values():
                if node.status != NodeStatus.ONLINE:
                    continue
                if capability and capability not in node.capabilities:
                    continue
                if node.current_load >= node.max_load:
                    continue
                nodes.append(node)
            return nodes

    async def update_load(self, node_id: str, delta: int) -> None:
        """更新节点负载。"""
        async with self._lock:
            if node := self._nodes.get(node_id):
                node.current_load = max(0, node.current_load + delta)

    async def check_timeouts(self, timeout_seconds: float = 30) -> list[str]:
        """检查超时节点，返回离线节点 ID。"""
        now = time.time()
        offline = []

        async with self._lock:
            for node_id, node in self._nodes.items():
                if now - node.last_heartbeat > timeout_seconds:
                    node.status = NodeStatus.OFFLINE
                    offline.append(node_id)

        return offline


class DistributedExecutor:
    """分布式执行器：协调多节点任务执行。"""

    def __init__(
        self,
        node_id: str,
        capabilities: list[str],
        task_queue: TaskQueue,
        node_registry: NodeRegistry,
    ) -> None:
        """初始化。

        Args:
            node_id: 本节点 ID
            capabilities: 能力标签
            task_queue: 任务队列
            node_registry: 节点注册表
        """
        self.node_id = node_id
        self.capabilities = capabilities
        self.task_queue = task_queue
        self.registry = node_registry
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._executor_task: asyncio.Task | None = None

    async def start(self, handler: Callable[[DistributedTask], Any]) -> None:
        """启动执行器。

        Args:
            handler: 任务处理函数
        """
        self._running = True

        # 注册节点
        await self.registry.register(NodeInfo(
            node_id=self.node_id,
            address="localhost",
            capabilities=self.capabilities,
        ))

        # 启动心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # 启动任务执行循环
        self._executor_task = asyncio.create_task(self._execute_loop(handler))

    async def stop(self) -> None:
        """停止执行器。"""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        if self._executor_task:
            self._executor_task.cancel()

        await self.registry.unregister(self.node_id)

    async def _heartbeat_loop(self) -> None:
        """心跳循环。"""
        while self._running:
            await self.registry.heartbeat(self.node_id)
            await asyncio.sleep(5)

    async def _execute_loop(self, handler: Callable[[DistributedTask], Any]) -> None:
        """任务执行循环。"""
        while self._running:
            # 获取任务
            task = await self.task_queue.get_task(self.capabilities)
            if task is None:
                await asyncio.sleep(1)
                continue

            # 更新负载
            await self.registry.update_load(self.node_id, 1)
            task.assigned_node = self.node_id

            try:
                # 执行任务
                result = handler(task)
                if asyncio.iscoroutine(result):
                    result = await result

                await self.task_queue.complete(task.id, result)

            except Exception as e:
                await self.task_queue.fail(task.id, str(e))

            finally:
                await self.registry.update_load(self.node_id, -1)


class ClusterManager:
    """集群管理器：协调多个分布式执行器。"""

    def __init__(self) -> None:
        """初始化集群管理器。"""
        self.task_queue = TaskQueue()
        self.registry = NodeRegistry()
        self._executors: list[DistributedExecutor] = []

    def create_executor(
        self,
        node_id: str | None = None,
        capabilities: list[str] | None = None,
    ) -> DistributedExecutor:
        """创建执行器。

        Args:
            node_id: 节点 ID（默认自动生成）
            capabilities: 能力标签

        Returns:
            分布式执行器
        """
        executor = DistributedExecutor(
            node_id=node_id or f"node_{uuid.uuid4().hex[:8]}",
            capabilities=capabilities or ["general"],
            task_queue=self.task_queue,
            node_registry=self.registry,
        )
        self._executors.append(executor)
        return executor

    async def submit_task(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 0,
    ) -> str:
        """提交任务。

        Args:
            task_type: 任务类型
            payload: 任务数据
            priority: 优先级

        Returns:
            任务 ID
        """
        task = DistributedTask(
            id=f"task_{uuid.uuid4().hex[:8]}",
            task_type=task_type,
            payload=payload,
            priority=priority,
        )
        await self.task_queue.submit(task)
        return task.id

    async def get_task_result(self, task_id: str, timeout: float = 60) -> Any:
        """获取任务结果。

        Args:
            task_id: 任务 ID
            timeout: 超时秒数

        Returns:
            任务结果
        """
        start = time.time()
        while time.time() - start < timeout:
            async with self.task_queue._lock:
                if task := self.task_queue._tasks.get(task_id):
                    if task.status == "completed":
                        return task.result
                    if task.status == "failed":
                        raise RuntimeError(task.error)
            await asyncio.sleep(0.5)

        raise TimeoutError(f"Task timeout: {task_id}")

    async def get_cluster_stats(self) -> dict:
        """获取集群统计。"""
        nodes = await self.registry.get_available_nodes()
        return {
            "total_nodes": len(nodes),
            "total_load": sum(n.current_load for n in nodes),
            "capabilities": list(set(
                cap for n in nodes for cap in n.capabilities
            )),
        }
