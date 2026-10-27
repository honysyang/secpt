"""检查点管理：自动定期保存快照。

从 snapshot.py 拆分而来，职责单一：
- SnapshotStore 负责快照的存取
- CheckpointManager 负责周期性自动保存与旧快照清理
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from harness.persistence.snapshot import SnapshotStore


class CheckpointManager:
    """检查点管理：自动定期保存快照。"""

    def __init__(
        self,
        snapshot_store: SnapshotStore,
        interval_seconds: float = 300,  # 5 分钟
        max_checkpoints: int = 10,
    ) -> None:
        """初始化。

        Args:
            snapshot_store: 快照存储
            interval_seconds: 自动保存间隔
            max_checkpoints: 最大保留检查点数
        """
        self.store = snapshot_store
        self.interval = interval_seconds
        self.max_checkpoints = max_checkpoints
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self, get_state: Callable[[], dict]) -> None:
        """启动自动检查点。

        Args:
            get_state: 获取当前状态的函数
        """
        self._running = True
        self._task = asyncio.create_task(self._checkpoint_loop(get_state))

    async def stop(self) -> None:
        """停止自动检查点。"""
        self._running = False
        if self._task:
            self._task.cancel()

    async def _checkpoint_loop(self, get_state: Callable[[], dict]) -> None:
        """检查点循环。"""
        while self._running:
            await asyncio.sleep(self.interval)

            # 获取状态并保存
            state = get_state()
            await self.store.save(**state)

            # 清理旧检查点
            await self._cleanup_old_checkpoints()

    async def _cleanup_old_checkpoints(self) -> None:
        """清理旧检查点。"""
        snapshots = await self.store.list_snapshots(limit=self.max_checkpoints + 1)
        for snap in snapshots[self.max_checkpoints:]:
            await self.store.delete_snapshot(snap["id"])
