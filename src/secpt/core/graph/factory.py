"""GraphStore 工厂。

``backend == "memory"`` 返回 :class:`MemoryGraphStore`；
sqlite / postgres 后端留待 Step 4b / 4c 实现。
"""

from __future__ import annotations

from secpt.config.models import GraphConfig
from secpt.core.graph.base import GraphStore
from secpt.core.graph.memory import MemoryGraphStore


def get_graph_store(cfg: GraphConfig) -> GraphStore:
    """按配置返回图存储实例。"""
    if cfg.backend == "memory":
        return MemoryGraphStore()
    if cfg.backend == "sqlite":
        from secpt.core.graph.sqlite import SQLiteGraphStore

        return SQLiteGraphStore(cfg.sqlite_path)
    if cfg.backend == "postgres":
        from secpt.core.graph.postgres import PostgresGraphStore

        if not cfg.dsn:
            raise ValueError("postgres backend 需要 dsn（走环境变量，勿写配置）")
        return PostgresGraphStore(cfg.dsn)
    raise NotImplementedError(
        f"GraphStore backend '{cfg.backend}' 尚未实现"
    )


__all__ = ["get_graph_store"]
