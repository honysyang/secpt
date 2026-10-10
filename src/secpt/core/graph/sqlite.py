"""SQLite GraphStore 持久化实现。

表结构：
- entities(eid PK, etype, props JSON, updated_at)
- edges(src, dst, etype, confidence, created_at, PK(src,dst,etype))
- evidences(evidence_id PK, eid, payload, trust_tier, trust_score, created_at)

并发安全：WAL 模式 + asyncio 写锁，支持 7×24 多协程并发写。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from secpt.core.graph.base import TRUST_SCORES, GraphStore, TrustTier

logger = structlog.get_logger(__name__)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class SQLiteGraphStore(GraphStore):
    """基于 SQLite 的持久化图存储（并发安全）。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            timeout=30.0,  # 锁等待 30 秒
        )
        self._conn.row_factory = sqlite3.Row
        # WAL 模式：读写不互斥
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 立即写入，防断电
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 写锁：防止多协程竞态
        self._write_lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                eid        TEXT PRIMARY KEY,
                etype      TEXT NOT NULL,
                props      TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edges (
                src        TEXT NOT NULL,
                dst        TEXT NOT NULL,
                etype      TEXT NOT NULL,
                confidence REAL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (src, dst, etype)
            );
            CREATE TABLE IF NOT EXISTS evidences (
                evidence_id TEXT PRIMARY KEY,
                eid         TEXT NOT NULL,
                payload     TEXT NOT NULL,
                trust_tier  TEXT NOT NULL,
                trust_score REAL NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
            CREATE INDEX IF NOT EXISTS idx_evidences_eid ON evidences(eid);
        """)
        self._conn.commit()

    async def upsert_entity(
        self,
        eid: str,
        etype: str,
        props: dict | None = None,
    ) -> None:
        now = _utcnow()
        props_json = json.dumps(props or {}, ensure_ascii=False)
        async with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO entities (eid, etype, props, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(eid) DO UPDATE SET
                    etype = excluded.etype,
                    props = excluded.props,
                    updated_at = excluded.updated_at
                """,
                (eid, etype, props_json, now),
            )
            self._conn.commit()
        logger.debug("graph.sqlite.entity.upsert", eid=eid, etype=etype)

    async def add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        confidence: float | None = None,
    ) -> None:
        async with self._write_lock:
            # 确保端点存在
            for eid in (src, dst):
                self._conn.execute(
                    "INSERT OR IGNORE INTO entities (eid, etype, props, updated_at) VALUES (?, 'unknown', '{}', ?)",
                    (eid, _utcnow()),
                )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO edges (src, dst, etype, confidence, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (src, dst, etype, confidence, _utcnow()),
            )
            self._conn.commit()
        logger.debug("graph.sqlite.edge.add", src=src, dst=dst, etype=etype)

    async def add_evidence(
        self,
        eid: str,
        payload: str,
        trust_tier: TrustTier = "deterministic",
        *,
        result: str = "ok",
    ) -> str:
        evidence_id = f"ev.{uuid.uuid4().hex[:12]}"
        score = TRUST_SCORES[trust_tier]
        async with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO evidences (evidence_id, eid, payload, trust_tier, trust_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (evidence_id, eid, payload, trust_tier, score, _utcnow()),
            )
            self._conn.commit()
        logger.debug(
            "graph.sqlite.evidence.add",
            evidence_id=evidence_id,
            eid=eid,
            trust_tier=trust_tier,
            result=result,
        )
        return evidence_id

    async def neighbors(self, eid: str) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            """
            SELECT e.dst, e.etype AS edge_type, e.confidence,
                   ent.etype, ent.props
            FROM edges e
            JOIN entities ent ON e.dst = ent.eid
            WHERE e.src = ?
            """,
            (eid,),
        )
        results: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            results.append(
                {
                    "eid": row["dst"],
                    "etype": row["etype"],
                    "props": json.loads(row["props"]),
                    "edge_type": row["edge_type"],
                    "confidence": row["confidence"],
                }
            )
        return results

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    # ---- 测试/调试用辅助 ----

    def get_entity(self, eid: str) -> dict[str, Any] | None:
        cursor = self._conn.execute("SELECT * FROM entities WHERE eid = ?", (eid,))
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "eid": row["eid"],
            "etype": row["etype"],
            "props": json.loads(row["props"]),
            "updated_at": row["updated_at"],
        }

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute("SELECT * FROM evidences WHERE evidence_id = ?", (evidence_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def has_edge(self, src: str, dst: str, etype: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND etype = ?",
            (src, dst, etype),
        )
        return cursor.fetchone() is not None
