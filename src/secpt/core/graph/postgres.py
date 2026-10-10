"""Postgres GraphStore 持久化实现。

表结构与 SQLite 版一致，使用 asyncpg 异步驱动。
连接通过 DSN 字符串，凭证明走环境变量，绝不写进配置。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from secpt.core.graph.base import TRUST_SCORES, GraphStore, TrustTier

logger = structlog.get_logger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresGraphStore(GraphStore):
    """基于 PostgreSQL 的持久化图存储。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    async def _get_pool(self):
        """惰性创建连接池。"""
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            await self._init_schema()
        return self._pool

    async def _init_schema(self) -> None:
        pool = self._pool
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS entities (
                    eid        TEXT PRIMARY KEY,
                    etype      TEXT NOT NULL,
                    props      JSONB NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edges (
                    src        TEXT NOT NULL,
                    dst        TEXT NOT NULL,
                    etype      TEXT NOT NULL,
                    confidence REAL,
                    created_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (src, dst, etype)
                );
                CREATE TABLE IF NOT EXISTS evidences (
                    evidence_id TEXT PRIMARY KEY,
                    eid         TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    trust_tier  TEXT NOT NULL,
                    trust_score REAL NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
                CREATE INDEX IF NOT EXISTS idx_evidences_eid ON evidences(eid);
            """)

    async def upsert_entity(
        self,
        eid: str,
        etype: str,
        props: dict | None = None,
    ) -> None:
        pool = await self._get_pool()
        props_json = json.dumps(props or {}, ensure_ascii=False)
        now = _utcnow()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO entities (eid, etype, props, updated_at)
                VALUES ($1, $2, $3::jsonb, $4)
                ON CONFLICT(eid) DO UPDATE SET
                    etype = EXCLUDED.etype,
                    props = EXCLUDED.props,
                    updated_at = EXCLUDED.updated_at
            """, eid, etype, props_json, now)
        logger.debug("graph.postgres.entity.upsert", eid=eid, etype=etype)

    async def add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        confidence: float | None = None,
    ) -> None:
        pool = await self._get_pool()
        now = _utcnow()
        async with pool.acquire() as conn:
            # 确保端点存在
            for eid in (src, dst):
                await conn.execute("""
                    INSERT INTO entities (eid, etype, props, updated_at)
                    VALUES ($1, 'unknown', '{}', $2)
                    ON CONFLICT(eid) DO NOTHING
                """, eid, now)
            await conn.execute("""
                INSERT INTO edges (src, dst, etype, confidence, created_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT(src, dst, etype) DO NOTHING
            """, src, dst, etype, confidence, now)
        logger.debug("graph.postgres.edge.add", src=src, dst=dst, etype=etype)

    async def add_evidence(
        self,
        eid: str,
        payload: str,
        trust_tier: TrustTier = "deterministic",
    ) -> str:
        pool = await self._get_pool()
        evidence_id = f"ev.{uuid.uuid4().hex[:12]}"
        score = TRUST_SCORES[trust_tier]
        now = _utcnow()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO evidences (evidence_id, eid, payload, trust_tier, trust_score, created_at)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, evidence_id, eid, payload, trust_tier, score, now)
        logger.debug("graph.postgres.evidence.add", evidence_id=evidence_id, eid=eid)
        return evidence_id

    async def neighbors(self, eid: str) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT e.dst, e.etype AS edge_type, e.confidence,
                       ent.etype, ent.props
                FROM edges e
                JOIN entities ent ON e.dst = ent.eid
                WHERE e.src = $1
            """, eid)
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append({
                "eid": row["dst"],
                "etype": row["etype"],
                "props": json.loads(row["props"]) if isinstance(row["props"], str) else row["props"],
                "edge_type": row["edge_type"],
                "confidence": row["confidence"],
            })
        return results

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None


__all__ = ["PostgresGraphStore"]
