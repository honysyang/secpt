"""快照恢复（runtime/recovery.py）：restore + 租约重算。

resume 是 run() 对已存在目录的入口：账本重放 → meta 由 derive_stats 重算
（不依赖内存计数器）→ 过期租约回收。孤儿进程 v0 不存在（worker 同步执行）。
"""
from __future__ import annotations

from pathlib import Path

from pa.models import MissionState
from pa.runtime.lease import expire_leases
from pa.store.view import StoreView
from pa.store.writer import StoreWriter


def resume(root) -> tuple[MissionState, StoreWriter]:
    writer = StoreWriter(Path(root))
    state = StoreView(writer.root).restore()   # 快照 + 事件重放 + meta 重算
    expire_leases(state, writer)
    return state, writer
