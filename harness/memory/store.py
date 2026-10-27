"""记忆存储：短期对话历史 + 长期向量存储。

支持：
- InMemoryMemory: 内存存储（开发用）
- SQLiteMemory: SQLite 持久化（生产可用）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryRecord:
    """单条记忆记录。"""

    key: str
    content: str
    kind: str = "fact"  # fact | preference | summary
    namespace: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class SQLiteMemory:
    """SQLite 记忆存储（生产可用）。

    注意：需要 sqlite3 支持，如果系统缺少 sqlite3 会报错。
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        # 延迟导入，避免在没有 sqlite3 的环境中报错
        import sqlite3

        self._sqlite3 = sqlite3
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """初始化表结构。"""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                content TEXT NOT NULL,
                kind TEXT DEFAULT 'fact',
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(namespace, key)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace)
        """)
        self._conn.commit()

    def remember(self, record: MemoryRecord) -> None:
        """存储或覆盖一条记忆。"""
        from datetime import datetime

        now = datetime.utcnow().isoformat()
        self._conn.execute("""
            INSERT OR REPLACE INTO memories
            (namespace, key, content, kind, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            record.namespace,
            record.key,
            record.content,
            record.kind,
            json.dumps(record.metadata),
            now,
            now,
        ))
        self._conn.commit()

    def recall(self, key: str, namespace: str = "default") -> MemoryRecord | None:
        """按 key 检索记忆。"""
        row = self._conn.execute(
            "SELECT * FROM memories WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def forget(self, key: str, namespace: str = "default") -> bool:
        """删除记忆，返回是否成功。"""
        cursor = self._conn.execute(
            "DELETE FROM memories WHERE namespace = ? AND key = ?",
            (namespace, key),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_all(self, namespace: str = "default") -> list[MemoryRecord]:
        """列出某命名空间下所有记忆。"""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE namespace = ? ORDER BY updated_at DESC",
            (namespace,),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def search(self, query: str, namespace: str = "default", limit: int = 10) -> list[MemoryRecord]:
        """模糊搜索记忆内容。"""
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE namespace = ? AND content LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (namespace, f"%{query}%", limit),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def _row_to_record(self, row: Any) -> MemoryRecord:
        """将数据库行转换为 MemoryRecord。"""
        return MemoryRecord(
            key=row["key"],
            content=row["content"],
            kind=row["kind"],
            namespace=row["namespace"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def close(self) -> None:
        """关闭连接。"""
        self._conn.close()


class InMemoryMemory:
    """内存记忆存储（开发用，生产换 SQLiteMemory）。"""

    def __init__(self) -> None:
        self._store: dict[str, MemoryRecord] = {}

    def remember(self, record: MemoryRecord) -> None:
        """存储或覆盖一条记忆。"""
        ns_key = f"{record.namespace}:{record.key}"
        self._store[ns_key] = record

    def recall(self, key: str, namespace: str = "default") -> MemoryRecord | None:
        """按 key 检索记忆。"""
        return self._store.get(f"{namespace}:{key}")

    def forget(self, key: str, namespace: str = "default") -> bool:
        """删除记忆，返回是否成功。"""
        ns_key = f"{namespace}:{key}"
        if ns_key in self._store:
            del self._store[ns_key]
            return True
        return False

    def list_all(self, namespace: str = "default") -> list[MemoryRecord]:
        """列出某命名空间下所有记忆。"""
        prefix = f"{namespace}:"
        return [v for k, v in self._store.items() if k.startswith(prefix)]
