"""共享黑板：多智能体协同工作的共享状态机制。

Blackboard 模式：
- 多个 Agent 可以读写共享状态
- 支持订阅状态变更（事件驱动）
- 支持锁机制防止竞态条件
- 支持命名空间隔离

典型用法：
    blackboard = Blackboard()

    # Agent A 写入发现
    await blackboard.write("findings", "关键发现", agent="researcher")

    # Agent B 读取并基于它工作
    findings = await blackboard.read("findings")

    # 订阅变更
    blackboard.subscribe("findings", callback)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class BlackboardEntry:
    """黑板条目。"""

    key: str
    value: Any
    agent: str  # 写入者
    timestamp: float = field(default_factory=time.time)
    version: int = 1  # 乐观锁版本号


@dataclass
class BlackboardEvent:
    """黑板事件。"""

    type: str  # write | delete | lock | unlock
    key: str
    value: Any = None
    agent: str = ""
    timestamp: float = field(default_factory=time.time)


class Blackboard:
    """共享黑板：多 Agent 协同的共享状态存储。

    特性：
    - 读写操作
    - 事件订阅（状态变更通知）
    - 命名空间隔离
    - 乐观锁（版本控制）
    - 异步锁（防止竞态）
    """

    def __init__(self) -> None:
        self._store: dict[str, BlackboardEntry] = {}
        self._subscribers: dict[str, list[Callable]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._history: list[BlackboardEvent] = []
        self._global_lock = asyncio.Lock()

    async def write(self, key: str, value: Any, agent: str = "system") -> BlackboardEntry:
        """写入黑板。

        Args:
            key: 键名
            value: 值
            agent: 写入者标识

        Returns:
            BlackboardEntry 写入的条目
        """
        async with self._global_lock:
            # 检查是否存在（用于版本控制）
            existing = self._store.get(key)
            version = existing.version + 1 if existing else 1

            entry = BlackboardEntry(
                key=key,
                value=value,
                agent=agent,
                version=version,
            )
            self._store[key] = entry

            # 记录事件
            event = BlackboardEvent(
                type="write",
                key=key,
                value=value,
                agent=agent,
            )
            self._history.append(event)

        # 通知订阅者（在锁外执行，避免死锁）
        await self._notify_subscribers(key, event)

        return entry

    async def read(self, key: str) -> Any | None:
        """读取黑板值。"""
        entry = self._store.get(key)
        return entry.value if entry else None

    async def read_entry(self, key: str) -> BlackboardEntry | None:
        """读取完整条目（含元数据）。"""
        return self._store.get(key)

    async def delete(self, key: str, agent: str = "system") -> bool:
        """删除黑板条目。"""
        async with self._global_lock:
            if key not in self._store:
                return False

            del self._store[key]

            event = BlackboardEvent(
                type="delete",
                key=key,
                agent=agent,
            )
            self._history.append(event)

        await self._notify_subscribers(key, event)
        return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        """列出所有键（支持前缀过滤）。"""
        if not prefix:
            return list(self._store.keys())
        return [k for k in self._store if k.startswith(prefix)]

    async def exists(self, key: str) -> bool:
        """检查键是否存在。"""
        return key in self._store

    def subscribe(self, key: str, callback: Callable[[BlackboardEvent], Any]) -> None:
        """订阅指定键的变更事件。

        Args:
            key: 要监听的键
            callback: 回调函数，接收 BlackboardEvent
        """
        if key not in self._subscribers:
            self._subscribers[key] = []
        self._subscribers[key].append(callback)

    def unsubscribe(self, key: str, callback: Callable) -> None:
        """取消订阅。"""
        if key in self._subscribers:
            self._subscribers[key] = [cb for cb in self._subscribers[key] if cb != callback]

    async def acquire_lock(self, key: str, timeout: float = 10.0) -> bool:
        """获取键锁（防止竞态条件）。

        Args:
            key: 要锁定的键
            timeout: 超时时间

        Returns:
            是否成功获取锁
        """
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()

        try:
            await asyncio.wait_for(self._locks[key].acquire(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def release_lock(self, key: str) -> None:
        """释放键锁。"""
        if key in self._locks and self._locks[key].locked():
            self._locks[key].release()

    async def get_history(self, key: str | None = None) -> list[BlackboardEvent]:
        """获取事件历史。"""
        if key is None:
            return self._history.copy()
        return [e for e in self._history if e.key == key]

    async def clear(self) -> None:
        """清空黑板。"""
        async with self._global_lock:
            self._store.clear()
            self._history.clear()
            # 不清理订阅者，保持监听

    async def _notify_subscribers(self, key: str, event: BlackboardEvent) -> None:
        """通知订阅者。"""
        if key not in self._subscribers:
            return

        for callback in self._subscribers[key]:
            try:
                result = callback(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                # 忽略订阅者错误，避免影响主流程
                pass


class BlackboardAgent:
    """支持黑板交互的 Agent。

    扩展 SubAgent，增加黑板读写能力。
    """

    def __init__(
        self,
        name: str,
        instructions: str,
        llm: Any,
        blackboard: Blackboard,
        tools: Any | None = None,
        read_keys: list[str] | None = None,
        write_keys: list[str] | None = None,
    ) -> None:
        """初始化。

        Args:
            name: Agent 名称
            instructions: 角色指令
            llm: LLM 客户端
            blackboard: 共享黑板
            tools: 工具集
            read_keys: 启动时要读取的键
            write_keys: 允许写入的键
        """
        from runtime.agents.base import SubAgent

        self._agent = SubAgent(name, instructions, llm, tools)
        self.blackboard = blackboard
        self.name = name
        self.read_keys = read_keys or []
        self.write_keys = write_keys or []

    async def run(self, task: str, context: str = "") -> Any:
        """执行任务，自动集成黑板上下文。"""
        # 1. 读取黑板上下文
        bb_context = await self._gather_context()

        # 2. 合并上下文
        full_context = f"{context}\n\n{bb_context}" if bb_context else context

        # 3. 执行任务
        result = await self._agent.run(task, context=full_context)

        # 4. 写回结果到黑板
        await self._write_results(task, result)

        return result

    async def run_simple(self, task: str, context: str = "") -> Any:
        """单次执行，集成黑板。"""
        bb_context = await self._gather_context()
        full_context = f"{context}\n\n{bb_context}" if bb_context else context
        result = await self._agent.run_simple(task, context=full_context)
        await self._write_results(task, result)
        return result

    async def read_blackboard(self, key: str) -> Any | None:
        """读取黑板值。"""
        return await self.blackboard.read(key)

    async def write_blackboard(self, key: str, value: Any) -> BlackboardEntry:
        """写入黑板（检查权限）。"""
        if self.write_keys and key not in self.write_keys:
            raise PermissionError(f"Agent '{self.name}' 不允许写入键 '{key}'")
        return await self.blackboard.write(key, value, agent=self.name)

    async def _gather_context(self) -> str:
        """收集黑板上下文。"""
        if not self.read_keys:
            return ""

        parts = []
        for key in self.read_keys:
            value = await self.blackboard.read(key)
            if value is not None:
                parts.append(f"[{key}]: {value}")

        return "共享黑板状态:\n" + "\n".join(parts) if parts else ""

    async def _write_results(self, task: str, result: Any) -> None:
        """写回结果到黑板。"""
        if not self.write_keys:
            return

        # 自动写入到第一个允许写入的键
        output = result.output if hasattr(result, 'output') else str(result)
        for key in self.write_keys:
            await self.blackboard.write(key, output, agent=self.name)
            break  # 只写入第一个键
