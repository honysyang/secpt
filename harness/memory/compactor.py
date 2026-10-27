"""冷节点压缩：长期未访问的记忆自动摘要。

借鉴 ARTEX Compactor 设计：
- 跟踪节点访问时间
- 长时间未访问的节点标记为"冷"
- 冷节点折叠为 LLM 生成的摘要
- 释放原始内容内存
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ColdStamp:
    """冷节点标记。"""

    node_id: str
    last_access: float  # 最后访问时间
    access_count: int = 0  # 访问次数
    is_cold: bool = False  # 是否已标记为冷
    cold_since: float | None = None  # 变冷时间
    digest: str | None = None  # 摘要内容


@dataclass
class CompactionStats:
    """压缩统计。"""

    total_nodes: int
    cold_nodes: int
    compacted_nodes: int
    memory_saved_bytes: int
    avg_cold_age_seconds: float


class Compactor:
    """冷节点压缩器。

    工作流程：
    1. 定期扫描所有节点
    2. 超过 cold_threshold 未访问的标记为冷
    3. 冷节点调用 LLM 生成摘要
    4. 释放原始内容，保留摘要
    """

    def __init__(
        self,
        llm: Any,
        cold_threshold_seconds: float = 3600,  # 1 小时
        compaction_interval_seconds: float = 300,  # 5 分钟
        min_access_count: int = 3,  # 至少访问 N 次才压缩（避免误压重要内容）
    ) -> None:
        """初始化压缩器。

        Args:
            llm: LLM 客户端（用于生成摘要）
            cold_threshold_seconds: 超过 N 秒未访问标记为冷
            compaction_interval_seconds: 压缩间隔
            min_access_count: 最少访问次数才压缩
        """
        self.llm = llm
        self.cold_threshold = cold_threshold_seconds
        self.compaction_interval = compaction_interval_seconds
        self.min_access_count = min_access_count

        self._stamps: dict[str, ColdStamp] = {}
        self._original_sizes: dict[str, int] = {}  # 原始内容大小
        self._compacted: set[str] = set()  # 已压缩的节点

    def record_access(self, node_id: str) -> None:
        """记录节点访问。

        Args:
            node_id: 节点 ID
        """
        if stamp := self._stamps.get(node_id):
            stamp.last_access = time.time()
            stamp.access_count += 1
            # 访问后取消冷标记（重新变热）
            if stamp.is_cold:
                stamp.is_cold = False
                stamp.cold_since = None
        else:
            self._stamps[node_id] = ColdStamp(
                node_id=node_id,
                last_access=time.time(),
                access_count=1,
            )

    def mark_cold_nodes(self) -> list[str]:
        """标记冷节点。

        Returns:
            新标记为冷的节点 ID 列表
        """
        now = time.time()
        newly_cold = []

        for node_id, stamp in self._stamps.items():
            if stamp.is_cold or node_id in self._compacted:
                continue

            # 检查是否超过阈值
            if now - stamp.last_access > self.cold_threshold:
                stamp.is_cold = True
                stamp.cold_since = now
                newly_cold.append(node_id)

        return newly_cold

    async def compact_node(self, node_id: str, original_content: str) -> str | None:
        """压缩单个节点：生成摘要，释放原始内容。

        Args:
            node_id: 节点 ID
            original_content: 原始内容

        Returns:
            生成的摘要，失败返回 None
        """
        if node_id in self._compacted:
            return None

        # 检查访问次数
        stamp = self._stamps.get(node_id)
        if stamp and stamp.access_count < self.min_access_count:
            return None  # 访问太少，不压缩

        # 生成摘要
        try:
            prompt = f"""请将以下内容压缩为简短摘要，保留关键信息：

{original_content[:2000]}  # 限制输入长度

要求：
1. 不超过 200 字
2. 保留关键事实和结论
3. 删除冗余细节"""

            response = await self.llm.chat([
                {"role": "system", "content": "你是内容压缩器，生成简短摘要。"},
                {"role": "user", "content": prompt},
            ])
            digest = response["choices"][0]["message"]["content"]

            # 记录
            self._original_sizes[node_id] = len(original_content)
            self._compacted.add(node_id)

            if stamp := self._stamps.get(node_id):
                stamp.digest = digest

            return digest

        except Exception:
            return None

    async def compact_all(self, content_getter: callable) -> dict[str, str]:
        """压缩所有冷节点。

        Args:
            content_getter: 获取节点内容的函数 (node_id) -> content

        Returns:
            {node_id: digest} 压缩结果
        """
        cold_nodes = self.mark_cold_nodes()
        results = {}

        for node_id in cold_nodes:
            if content := content_getter(node_id):
                if digest := await self.compact_node(node_id, content):
                    results[node_id] = digest

        return results

    def get_node_content(self, node_id: str, original: str) -> str:
        """获取节点内容（如果是压缩节点，返回摘要）。

        Args:
            node_id: 节点 ID
            original: 原始内容

        Returns:
            摘要或原始内容
        """
        if node_id in self._compacted:
            if stamp := self._stamps.get(node_id):
                return stamp.digest or original
        return original

    def get_stats(self) -> CompactionStats:
        """获取压缩统计。"""
        now = time.time()
        cold_nodes = [s for s in self._stamps.values() if s.is_cold]
        compacted = len(self._compacted)

        total_saved = sum(self._original_sizes.get(nid, 0) for nid in self._compacted)

        avg_age = 0.0
        if cold_nodes:
            ages = [
                now - s.cold_since for s in cold_nodes if s.cold_since
            ]
            if ages:
                avg_age = sum(ages) / len(ages)

        return CompactionStats(
            total_nodes=len(self._stamps),
            cold_nodes=len(cold_nodes),
            compacted_nodes=compacted,
            memory_saved_bytes=total_saved,
            avg_cold_age_seconds=avg_age,
        )

    def is_compacted(self, node_id: str) -> bool:
        """检查节点是否已压缩。"""
        return node_id in self._compacted

    def restore_node(self, node_id: str) -> bool:
        """恢复节点（从压缩状态）。

        Args:
            node_id: 节点 ID

        Returns:
            是否成功恢复
        """
        if node_id not in self._compacted:
            return False

        self._compacted.discard(node_id)
        if stamp := self._stamps.get(node_id):
            stamp.is_cold = False
            stamp.cold_since = None
            stamp.digest = None
        return True


class SmartMemoryWithCompaction:
    """带压缩功能的智能记忆。

    结合 Compactor，自动管理记忆生命周期。
    """

    def __init__(
        self,
        llm: Any,
        cold_threshold_seconds: float = 3600,
        max_active_memories: int = 100,
    ) -> None:
        """初始化。

        Args:
            llm: LLM 客户端
            cold_threshold_seconds: 冷节点阈值
            max_active_memories: 最大活跃记忆数
        """
        self.llm = llm
        self.max_active = max_active_memories
        self.compactor = Compactor(
            llm,
            cold_threshold_seconds=cold_threshold_seconds,
            min_access_count=1,  # 智能记忆中，任何记忆都可压缩
        )

        self._memories: dict[str, str] = {}  # 活跃记忆
        self._compacted_memories: dict[str, str] = {}  # 压缩后的记忆（摘要）

    async def remember(self, key: str, content: str) -> None:
        """存储记忆。

        Args:
            key: 键
            content: 内容
        """
        # 检查容量
        if len(self._memories) >= self.max_active:
            # 尝试压缩最旧的记忆
            await self._compact_oldest()

        self._memories[key] = content
        self.compactor.record_access(key)

    async def recall(self, key: str) -> str | None:
        """回忆记忆。

        Args:
            key: 键

        Returns:
            内容（可能是摘要）
        """
        self.compactor.record_access(key)

        # 先查活跃记忆
        if key in self._memories:
            return self._memories[key]

        # 再查压缩记忆
        if key in self._compacted_memories:
            return self._compacted_memories[key]

        return None

    async def _compact_oldest(self) -> None:
        """压缩最旧的记忆。"""
        # 找到最久未访问的
        oldest_key = None
        oldest_time = float('inf')

        for key in self._memories:
            if stamp := self.compactor._stamps.get(key):
                if stamp.last_access < oldest_time:
                    oldest_time = stamp.last_access
                    oldest_key = key

        if oldest_key and oldest_key in self._memories:
            content = self._memories[oldest_key]

            # 压缩
            if digest := await self.compactor.compact_node(oldest_key, content):
                self._compacted_memories[oldest_key] = digest
                del self._memories[oldest_key]

    def get_stats(self) -> dict[str, Any]:
        """获取统计。"""
        compaction = self.compactor.get_stats()
        return {
            "active_memories": len(self._memories),
            "compacted_memories": len(self._compacted_memories),
            "total_capacity": self.max_active,
            **vars(compaction),
        }
