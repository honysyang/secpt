"""持久化状态：快照和恢复。

支持：
- 黑板状态快照（JSON / SQLite）
- 执行状态恢复
- 版本历史
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 延迟导入 sqlite3（某些系统可能不可用）
try:
    import sqlite3
    HAS_SQLITE = True
except ImportError:
    HAS_SQLITE = False


@dataclass
class Snapshot:
    """状态快照。"""

    id: str
    timestamp: float
    blackboard_state: dict[str, Any]
    frontier_state: dict[str, Any] | None = None
    execution_state: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SnapshotStore:
    """快照存储：保存和恢复系统状态。"""

    def __init__(self, storage_path: str | Path = "snapshots/") -> None:
        """初始化。

        Args:
            storage_path: 存储目录
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # JSON 文件存储（SQLite 可选）
        self.use_sqlite = HAS_SQLITE
        if self.use_sqlite:
            self.db_path = self.storage_path / "snapshots.db"
            self._init_db()
        else:
            self.db_path = None

    def _init_db(self) -> None:
        """初始化数据库。"""
        if not self.use_sqlite:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    timestamp REAL,
                    data TEXT,
                    metadata TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT,
                    action TEXT,
                    timestamp REAL
                )
            """)

    async def save(
        self,
        blackboard: Any,
        frontier: Any = None,
        execution_state: dict | None = None,
        metadata: dict | None = None,
    ) -> str:
        """保存快照。

        Args:
            blackboard: 黑板实例
            frontier: 意图队列（可选）
            execution_state: 执行状态（可选）
            metadata: 元数据（可选）

        Returns:
            快照 ID
        """
        snapshot_id = f"snap_{int(time.time() * 1000)}"

        # 收集黑板状态
        bb_state = await self._extract_blackboard_state(blackboard)

        # 收集 Frontier 状态
        frontier_state = None
        if frontier:
            frontier_state = await self._extract_frontier_state(frontier)

        snapshot = Snapshot(
            id=snapshot_id,
            timestamp=time.time(),
            blackboard_state=bb_state,
            frontier_state=frontier_state,
            execution_state=execution_state,
            metadata=metadata or {},
        )

        # 保存到 SQLite 或 JSON
        if self.use_sqlite:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?)",
                    (
                        snapshot.id,
                        snapshot.timestamp,
                        json.dumps(snapshot.__dict__, ensure_ascii=False),
                        json.dumps(snapshot.metadata, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "INSERT INTO snapshot_history VALUES (NULL, ?, ?, ?)",
                    (snapshot.id, "save", time.time()),
                )
        else:
            # JSON 文件存储
            snapshot_file = self.storage_path / f"{snapshot.id}.json"
            with open(snapshot_file, "w") as f:
                json.dump(snapshot.__dict__, f, ensure_ascii=False)

        return snapshot_id

    async def load(self, snapshot_id: str) -> Snapshot | None:
        """加载快照。

        Args:
            snapshot_id: 快照 ID

        Returns:
            快照对象，不存在返回 None
        """
        if self.use_sqlite:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT data FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None

                data = json.loads(row[0])
                return Snapshot(**data)
        else:
            snapshot_file = self.storage_path / f"{snapshot_id}.json"
            if not snapshot_file.exists():
                return None
            with open(snapshot_file) as f:
                data = json.load(f)
            return Snapshot(**data)

    async def restore_blackboard(
        self,
        snapshot_id: str,
        blackboard: Any,
    ) -> bool:
        """恢复黑板状态。

        Args:
            snapshot_id: 快照 ID
            blackboard: 黑板实例

        Returns:
            是否成功
        """
        snapshot = await self.load(snapshot_id)
        if snapshot is None:
            return False

        await self._apply_blackboard_state(blackboard, snapshot.blackboard_state)
        return True

    async def list_snapshots(self, limit: int = 10) -> list[dict]:
        """列出所有快照。

        Args:
            limit: 返回数量

        Returns:
            快照元数据列表
        """
        if self.use_sqlite:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT id, timestamp, metadata FROM snapshots ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
                return [
                    {
                        "id": row[0],
                        "timestamp": row[1],
                        "metadata": json.loads(row[2]),
                    }
                    for row in cursor.fetchall()
                ]
        else:
            # JSON 文件列表
            snapshots = []
            for file in sorted(
                self.storage_path.glob("*.json"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )[:limit]:
                with open(file) as f:
                    data = json.load(f)
                snapshots.append({
                    "id": data["id"],
                    "timestamp": data["timestamp"],
                    "metadata": data.get("metadata", {}),
                })
            return snapshots

    async def delete_snapshot(self, snapshot_id: str) -> bool:
        """删除快照。

        Args:
            snapshot_id: 快照 ID

        Returns:
            是否成功
        """
        if self.use_sqlite:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                )
                conn.execute(
                    "INSERT INTO snapshot_history VALUES (NULL, ?, ?, ?)",
                    (snapshot_id, "delete", time.time()),
                )
                return cursor.rowcount > 0
        else:
            snapshot_file = self.storage_path / f"{snapshot_id}.json"
            if snapshot_file.exists():
                snapshot_file.unlink()
                return True
            return False

    async def _extract_blackboard_state(self, blackboard: Any) -> dict:
        """提取黑板状态。"""
        state = {"type": type(blackboard).__name__}

        if hasattr(blackboard, "_global"):
            state["global"] = {
                k: {"value": v.value, "agent": v.agent}
                for k, v in blackboard._global.items()
            }

        if hasattr(blackboard, "_domains"):
            state["domains"] = {
                domain: {
                    k: {"value": v.value, "agent": v.agent}
                    for k, v in entries.items()
                }
                for domain, entries in blackboard._domains.items()
            }

        if hasattr(blackboard, "_nodes"):
            # GraphBlackboard
            state["graph_nodes"] = {
                node_id: {
                    "type": node.type,
                    "label": node.label,
                    "properties": node.properties,
                }
                for node_id, node in blackboard._nodes.items()
            }
            state["graph_edges"] = [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "type": edge.type,
                }
                for edge in blackboard._edges.values()
            ]

        return state

    async def _extract_frontier_state(self, frontier: Any) -> dict:
        """提取 Frontier 状态。"""
        intents = await frontier.get_all_intents()
        return {
            "intents": [
                {
                    "id": i.id,
                    "description": i.description,
                    "status": i.status,
                    "priority": i.priority,
                }
                for i in intents
            ]
        }

    async def _apply_blackboard_state(self, blackboard: Any, state: dict) -> None:
        """应用黑板状态。"""
        if "global" in state and hasattr(blackboard, "_global"):
            for key, data in state["global"].items():
                await blackboard.write_global(key, data["value"], agent=data["agent"])

        if "domains" in state and hasattr(blackboard, "_domains"):
            for domain, entries in state["domains"].items():
                for key, data in entries.items():
                    await blackboard.write_domain(domain, key, data["value"], agent=data["agent"])
