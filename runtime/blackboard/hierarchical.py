"""分层黑板：解决长时间、高复杂任务的上下文爆炸问题。

架构：
- Global Blackboard: 全局摘要，只存关键结论
- Domain Blackboard: 按领域/模块隔离
- Local Blackboard: Agent 私有，临时数据自动清理

特性：
- TTL 自动过期
- 容量限制
- 智能过滤
- 冲突检测（乐观锁）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from runtime.blackboard.base import Blackboard, BlackboardEntry, BlackboardEvent


class BlackboardError(Exception):
    """黑板基础异常。"""


class CapacityExceededError(BlackboardError):
    """容量超限。"""


class ConflictError(BlackboardError):
    """版本冲突。"""


class PermissionDeniedError(BlackboardError):
    """权限不足。"""


@dataclass
class TTLBlackboardEntry(BlackboardEntry):
    """带 TTL 的黑板条目。"""

    expire_at: float = 0  # 过期时间戳（0 = 永不过期）
    is_critical: bool = False  # 是否关键信息（不被自动清理）

    @property
    def is_expired(self) -> bool:
        """是否已过期（关键信息永不过期）。"""
        if self.is_critical or self.expire_at == 0:
            return False
        return time.time() > self.expire_at


@dataclass
class BlackboardLayer:
    """黑板层配置。"""

    name: str
    max_entries: int = 100  # 最大条目数
    max_value_size: int = 10000  # 单条最大字节数
    default_ttl: float = 300  # 默认 TTL（秒）


class HierarchicalBlackboard:
    """分层黑板：全局/领域/本地 三层架构。

    解决上下文爆炸、污染、竞态问题。
    """

    def __init__(
        self,
        global_max_entries: int = 50,
        domain_max_entries: int = 200,
        local_max_entries: int = 100,
    ) -> None:
        """初始化三层黑板。

        Args:
            global_max_entries: 全局层最大条目数
            domain_max_entries: 领域层每个域最大条目数
            local_max_entries: 本地层每个 Agent 最大条目数
        """
        # 三层存储
        self._global: dict[str, TTLBlackboardEntry] = {}
        self._domains: dict[str, dict[str, TTLBlackboardEntry]] = {}
        self._local: dict[str, dict[str, TTLBlackboardEntry]] = {}  # agent_id -> entries

        # 层配置
        self.global_layer = BlackboardLayer("global", global_max_entries, 5000, 3600)
        self.domain_layer = BlackboardLayer("domain", domain_max_entries, 10000, 1800)
        self.local_layer = BlackboardLayer("local", local_max_entries, 5000, 300)

        # 锁
        self._lock = asyncio.Lock()

        # 事件
        self._subscribers: dict[str, list[Callable]] = {}
        self._history: list[BlackboardEvent] = []

        # 清理任务
        self._cleanup_task: asyncio.Task | None = None
        self._cleanup_interval = 60  # 每 60 秒清理一次

    async def start_cleanup(self) -> None:
        """启动自动清理任务。"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup(self) -> None:
        """停止清理任务。"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        """定期清理过期条目。"""
        while True:
            await asyncio.sleep(self._cleanup_interval)
            await self._cleanup_expired()

    async def _cleanup_expired(self) -> None:
        """清理所有过期条目（保留关键信息）。"""
        async with self._lock:
            # 全局层
            expired = [
                k for k, v in self._global.items()
                if v.is_expired and not v.is_critical
            ]
            for k in expired:
                del self._global[k]

            # 领域层
            for domain, entries in self._domains.items():
                expired = [
                    k for k, v in entries.items()
                    if v.is_expired and not v.is_critical
                ]
                for k in expired:
                    del entries[k]

            # 本地层
            for agent_id, entries in self._local.items():
                expired = [
                    k for k, v in entries.items()
                    if v.is_expired and not v.is_critical
                ]
                for k in expired:
                    del entries[k]

    async def get_critical_entries(self) -> dict[str, Any]:
        """获取所有关键信息。"""
        async with self._lock:
            result = {}
            for key, entry in self._global.items():
                if entry.is_critical:
                    result[f"global:{key}"] = entry.value
            for domain, entries in self._domains.items():
                for key, entry in entries.items():
                    if entry.is_critical:
                        result[f"{domain}:{key}"] = entry.value
            return result

    async def pin_entry(self, layer: str, key: str, domain: str | None = None) -> bool:
        """将现有条目标记为关键信息（防止被清理）。

        Args:
            layer: 层名（global/domain/local）
            key: 键名
            domain: 领域名（layer=domain 时必填）
        """
        async with self._lock:
            entry = None
            if layer == "global":
                entry = self._global.get(key)
            elif layer == "domain" and domain:
                entry = self._domains.get(domain, {}).get(key)
            elif layer == "local":
                # 本地层需要 agent_id，简化处理
                pass

            if entry:
                entry.is_critical = True
                entry.expire_at = 0
                return True
            return False

    # ==================== 写入操作 ====================

    async def write_global(
        self,
        key: str,
        value: Any,
        agent: str = "system",
        ttl: float | None = None,
        expected_version: int | None = None,
        critical: bool = False,
    ) -> TTLBlackboardEntry:
        """写入全局黑板（关键结论、摘要）。

        Args:
            key: 键名
            value: 值
            agent: 写入者
            ttl: 过期时间（None = 使用默认，0 = 永不过期）
            expected_version: 期望版本（乐观锁）
            critical: 是否关键信息（永不过期，不被清理）
        """
        return await self._write(
            layer=self._global,
            layer_config=self.global_layer,
            key=key,
            value=value,
            agent=agent,
            ttl=ttl,
            expected_version=expected_version,
            critical=critical,
        )

    async def write_critical(
        self,
        key: str,
        value: Any,
        agent: str,
        reason: str,
        domains: list[str] | None = None,
    ) -> TTLBlackboardEntry:
        """写入关键信息：全局保留 + 可选广播到领域。

        Args:
            key: 键名
            value: 值
            agent: 写入者
            reason: 关键原因（元数据）
            domains: 可选，广播到指定领域
        """
        # 写入全局（永不过期）
        entry = await self.write_global(
            f"critical_{key}",
            value,
            agent=agent,
            ttl=0,  # 永不过期
            critical=True,
        )

        # 更新元数据
        entry.value = {
            "data": value,
            "reason": reason,
            "reported_by": agent,
            "timestamp": time.time(),
        }

        # 可选广播
        if domains:
            for domain in domains:
                await self.broadcast_to_domain(
                    source_agent=agent,
                    key=f"alert_{key}",
                    value=value,
                    reason=reason,
                    target_domain=domain,
                )

        return entry

    async def broadcast_to_domain(
        self,
        source_agent: str,
        key: str,
        value: Any,
        reason: str,
        target_domain: str,
        ttl: float = 86400,  # 默认 24 小时
    ) -> TTLBlackboardEntry:
        """跨领域广播关键信息。

        Args:
            source_agent: 来源 Agent
            key: 键名
            value: 值
            reason: 广播原因
            target_domain: 目标领域
            ttl: 过期时间
        """
        broadcast_value = {
            "data": value,
            "source": source_agent,
            "reason": reason,
            "original_key": key,
            "timestamp": time.time(),
        }

        return await self.write_domain(
            domain=target_domain,
            key=key,
            value=broadcast_value,
            agent=source_agent,
            ttl=ttl,
            critical=True,  # 广播信息也是关键信息
        )

    async def write_domain(
        self,
        domain: str,
        key: str,
        value: Any,
        agent: str = "system",
        ttl: float | None = None,
        expected_version: int | None = None,
        critical: bool = False,
    ) -> TTLBlackboardEntry:
        """写入领域黑板（按模块/主题隔离）。"""
        if domain not in self._domains:
            self._domains[domain] = {}

        return await self._write(
            layer=self._domains[domain],
            layer_config=self.domain_layer,
            key=key,
            value=value,
            agent=agent,
            ttl=ttl,
            expected_version=expected_version,
            critical=critical,
        )

    async def write_local(
        self,
        agent_id: str,
        key: str,
        value: Any,
        ttl: float | None = None,
        critical: bool = False,
    ) -> TTLBlackboardEntry:
        """写入本地黑板（Agent 私有，临时数据）。"""
        if agent_id not in self._local:
            self._local[agent_id] = {}

        return await self._write(
            layer=self._local[agent_id],
            layer_config=self.local_layer,
            key=key,
            value=value,
            agent=agent_id,
            ttl=ttl,
            critical=critical,
        )

    async def _write(
        self,
        layer: dict[str, TTLBlackboardEntry],
        layer_config: BlackboardLayer,
        key: str,
        value: Any,
        agent: str,
        ttl: float | None,
        expected_version: int | None = None,
        critical: bool = False,
    ) -> TTLBlackboardEntry:
        """底层写入实现。"""
        async with self._lock:
            # 容量检查（关键信息不受限制）
            if not critical and key not in layer and len(layer) >= layer_config.max_entries:
                raise CapacityExceededError(
                    f"层 '{layer_config.name}' 容量已满（{layer_config.max_entries}）"
                )

            # 大小检查
            value_size = len(str(value))
            if value_size > layer_config.max_value_size:
                raise ValueError(
                    f"值大小 {value_size} 超过限制 {layer_config.max_value_size}"
                )

            # 版本冲突检查
            existing = layer.get(key)
            if expected_version is not None:
                if existing is None:
                    raise ConflictError(f"键 '{key}' 不存在")
                if existing.version != expected_version:
                    raise ConflictError(
                        f"版本冲突：期望 {expected_version}，实际 {existing.version}"
                    )

            # 计算 TTL
            if critical:
                expire_at = 0  # 关键信息永不过期
            elif ttl is not None:
                expire_at = time.time() + ttl
            else:
                expire_at = time.time() + layer_config.default_ttl

            # 写入
            entry = TTLBlackboardEntry(
                key=key,
                value=value,
                agent=agent,
                version=existing.version + 1 if existing else 1,
                expire_at=expire_at,
                is_critical=critical,
            )
            layer[key] = entry

            # 记录事件
            event = BlackboardEvent(
                type="write",
                key=f"{layer_config.name}:{key}",
                value=value,
                agent=agent,
            )
            self._history.append(event)

        # 通知订阅者
        await self._notify(f"{layer_config.name}:{key}", event)

        return entry

    # ==================== 读取操作 ====================

    async def read_global(self, key: str) -> Any | None:
        """读取全局黑板。"""
        entry = self._global.get(key)
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def read_domain(self, domain: str, key: str) -> Any | None:
        """读取领域黑板。"""
        entries = self._domains.get(domain, {})
        entry = entries.get(key)
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def read_local(self, agent_id: str, key: str) -> Any | None:
        """读取本地黑板。"""
        entries = self._local.get(agent_id, {})
        entry = entries.get(key)
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def read_domain_all(self, domain: str) -> dict[str, Any]:
        """读取整个领域黑板（过滤过期）。"""
        entries = self._domains.get(domain, {})
        return {
            k: v.value for k, v in entries.items() if not v.is_expired
        }

    # ==================== 智能过滤读取 ====================

    async def read_relevant(
        self,
        query_keywords: list[str],
        agent_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """智能过滤：只读取与关键词相关的条目。

        Args:
            query_keywords: 查询关键词
            agent_id: 可选，同时读取该 Agent 的本地黑板
            limit: 返回条数限制

        Returns:
            过滤后的键值对
        """
        results = {}

        async with self._lock:
            # 全局层匹配
            for key, entry in self._global.items():
                if entry.is_expired:
                    continue
                if any(kw in key.lower() or kw in str(entry.value).lower() 
                       for kw in query_keywords):
                    results[f"global:{key}"] = entry.value

            # 领域层匹配
            for domain, entries in self._domains.items():
                for key, entry in entries.items():
                    if entry.is_expired:
                        continue
                    if any(kw in key.lower() or kw in str(entry.value).lower()
                           for kw in query_keywords):
                        results[f"{domain}:{key}"] = entry.value

            # 本地层（仅指定 Agent）
            if agent_id and agent_id in self._local:
                for key, entry in self._local[agent_id].items():
                    if entry.is_expired:
                        continue
                    if any(kw in key.lower() or kw in str(entry.value).lower()
                           for kw in query_keywords):
                        results[f"local:{key}"] = entry.value

        # 限制数量
        return dict(list(results.items())[:limit])

    # ==================== 其他操作 ====================

    async def delete_global(self, key: str, agent: str = "system") -> bool:
        """删除全局条目。"""
        async with self._lock:
            if key in self._global:
                del self._global[key]
                return True
            return False

    async def delete_domain(self, domain: str, key: str, agent: str = "system") -> bool:
        """删除领域条目。"""
        async with self._lock:
            entries = self._domains.get(domain, {})
            if key in entries:
                del entries[key]
                return True
            return False

    async def clear_local(self, agent_id: str) -> None:
        """清空 Agent 本地黑板。"""
        async with self._lock:
            if agent_id in self._local:
                self._local[agent_id].clear()

    async def get_stats(self) -> dict[str, Any]:
        """获取黑板统计。"""
        async with self._lock:
            return {
                "global_entries": len(self._global),
                "global_max": self.global_layer.max_entries,
                "domain_count": len(self._domains),
                "domain_entries": sum(len(d) for d in self._domains.values()),
                "agent_count": len(self._local),
                "local_entries": sum(len(l) for l in self._local.values()),
                "history_events": len(self._history),
            }

    def subscribe(self, key_pattern: str, callback: Callable) -> None:
        """订阅事件（支持层前缀，如 "global:*"）。"""
        if key_pattern not in self._subscribers:
            self._subscribers[key_pattern] = []
        self._subscribers[key_pattern].append(callback)

    async def _notify(self, key: str, event: BlackboardEvent) -> None:
        """通知订阅者。"""
        for pattern, callbacks in self._subscribers.items():
            # 简单匹配：精确匹配或通配符
            if pattern == key or pattern.endswith(":*") and key.startswith(pattern[:-1]):
                for cb in callbacks:
                    try:
                        result = cb(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        pass

    def export_state(self) -> dict[str, Any]:
        """导出黑板完整状态（全局/领域/本地三层）为可 JSON 序列化的字典。

        过期条目不导出（恢复时它们也没有价值）。
        """
        def _entries(entries: dict[str, TTLBlackboardEntry]) -> dict[str, Any]:
            # agent/version 必须导出——BlackboardEntry.agent 是必填字段，
            # 漏掉会导致 restore 时构造失败
            return {
                k: {
                    "key": e.key,
                    "value": e.value,
                    "agent": e.agent,
                    "timestamp": e.timestamp,
                    "version": e.version,
                    "expire_at": e.expire_at,
                    "is_critical": e.is_critical,
                }
                for k, e in entries.items()
                if not e.is_expired
            }

        return {
            "global": _entries(self._global),
            "domains": {
                d: _entries(entries) for d, entries in self._domains.items()
            },
            "local": {
                a: _entries(entries) for a, entries in self._local.items()
            },
        }

    def restore_state(self, data: dict[str, Any]) -> int:
        """从 export_state 的字典恢复黑板状态。

        Returns:
            恢复的条目数（跨会话记忆的量化指标）
        """
        def _load(
            target: dict[str, Any], entries: dict[str, Any]
        ) -> None:
            for k, raw in entries.items():
                target[k] = TTLBlackboardEntry(
                    key=raw["key"],
                    value=raw["value"],
                    agent=raw.get("agent", "system"),
                    timestamp=raw.get("timestamp", time.time()),
                    version=raw.get("version", 1),
                    expire_at=raw.get("expire_at", 0),
                    is_critical=raw.get("is_critical", False),
                )

        count = 0
        _load(self._global, data.get("global", {}))
        count += len(data.get("global", {}))
        for domain, entries in data.get("domains", {}).items():
            self._domains.setdefault(domain, {})
            _load(self._domains[domain], entries)
            count += len(entries)
        for agent_id, entries in data.get("local", {}).items():
            self._local.setdefault(agent_id, {})
            _load(self._local[agent_id], entries)
            count += len(entries)
        return count


class SmartBlackboardAgent:
    """智能黑板 Agent：自动过滤上下文，防止污染。"""

    def __init__(
        self,
        name: str,
        instructions: str,
        llm: Any,
        blackboard: HierarchicalBlackboard,
        domain: str | None = None,  # 所属领域
        read_global: bool = True,   # 是否读全局
        read_own_domain: bool = True,  # 是否读本领域
        context_limit: int = 5,     # 上下文条数限制
    ) -> None:
        """初始化。

        Args:
            name: Agent 名称
            instructions: 指令
            llm: LLM 客户端
            blackboard: 分层黑板
            domain: 所属领域（用于隔离）
            read_global: 是否读全局层
            read_own_domain: 是否读本领域层
            context_limit: 上下文条数限制
        """
        from runtime.agents.base import SubAgent

        self._agent = SubAgent(name, instructions, llm)
        self.name = name
        self.blackboard = blackboard
        self.domain = domain
        self.read_global = read_global
        self.read_own_domain = read_own_domain
        self.context_limit = context_limit

    async def run(self, task: str, context: str = "") -> Any:
        """执行任务，智能收集黑板上下文。"""
        # 1. 智能收集相关上下文
        bb_context = await self._gather_smart_context(task)

        # 2. 合并上下文
        full_context = f"{context}\n\n{bb_context}" if bb_context else context

        # 3. 执行任务
        result = await self._agent.run(task, context=full_context)

        # 4. 写回结果（自动路由到合适的层）
        await self._write_back(task, result)

        return result

    async def _gather_smart_context(self, task: str) -> str:
        """智能收集上下文：只读取相关条目。"""
        parts = []

        # 提取任务关键词（简单实现）
        keywords = [w for w in task.lower().split() if len(w) > 2]

        # 读全局层（只读关键结论）
        if self.read_global:
            global_summary = await self.blackboard.read_global("summary")
            if global_summary:
                parts.append(f"[全局摘要] {global_summary}")

        # 读本领域层（完整数据）
        if self.domain and self.read_own_domain:
            domain_data = await self.blackboard.read_domain_all(self.domain)
            # 只取最近 N 条
            for key, value in list(domain_data.items())[:self.context_limit]:
                parts.append(f"[{self.domain}:{key}] {str(value)[:200]}...")

        # 智能搜索相关条目
        if keywords:
            relevant = await self.blackboard.read_relevant(
                query_keywords=keywords,
                agent_id=self.name,
                limit=self.context_limit,
            )
            for key, value in relevant.items():
                if key not in str(parts):  # 去重
                    parts.append(f"[{key}] {str(value)[:200]}...")

        return "\n".join(parts[:self.context_limit * 2])  # 总体限制

    async def _write_back(self, task: str, result: Any) -> None:
        """写回结果到合适的层。"""
        output = result.output if hasattr(result, 'output') else str(result)

        # 简短结论 -> 全局层
        if len(output) < 500:
            await self.blackboard.write_global(
                f"{self.name}_conclusion",
                output,
                agent=self.name,
            )
        else:
            # 详细结果 -> 领域层
            domain = self.domain or "general"
            await self.blackboard.write_domain(
                domain,
                f"{self.name}_result",
                output,
                agent=self.name,
            )

        # 临时数据 -> 本地层（短 TTL）
        await self.blackboard.write_local(
            self.name,
            "last_task",
            task,
            ttl=60,  # 60 秒过期
        )
