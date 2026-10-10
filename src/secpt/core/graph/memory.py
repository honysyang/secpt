"""内存图存储实现（进程内 dict）。"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from secpt.core.graph.base import TRUST_SCORES, GraphStore, TrustTier

logger = structlog.get_logger(__name__)


class MemoryGraphStore(GraphStore):
    """进程内图存储，仅供单进程 / 测试使用。"""

    def __init__(self) -> None:
        self._entities: dict[str, dict[str, Any]] = {}
        self._edges: dict[str, list[dict[str, Any]]] = {}
        self._evidences: dict[str, dict[str, Any]] = {}

    async def upsert_entity(
        self,
        eid: str,
        etype: str,
        props: dict | None = None,
    ) -> None:
        existing = self._entities.get(eid, {})
        merged = {**existing, "eid": eid, "etype": etype, "props": props or {}}
        self._entities[eid] = merged
        logger.debug("graph.entity.upsert", eid=eid, etype=etype)

    async def add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        confidence: float | None = None,
    ) -> None:
        # 确保端点存在（即使还没显式 upsert）
        self._entities.setdefault(src, {"eid": src, "etype": "unknown", "props": {}})
        self._entities.setdefault(dst, {"eid": dst, "etype": "unknown", "props": {}})
        self._edges.setdefault(src, []).append(
            {"dst": dst, "etype": etype, "confidence": confidence}
        )
        logger.debug("graph.edge.add", src=src, dst=dst, etype=etype, confidence=confidence)

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
        self._evidences[evidence_id] = {
            "evidence_id": evidence_id,
            "eid": eid,
            "payload": payload,
            "trust_tier": trust_tier,
            "trust_score": score,
            "result": result,
        }
        logger.debug("graph.evidence.add", evidence_id=evidence_id, eid=eid, trust_tier=trust_tier, result=result)
        return evidence_id

    async def neighbors(self, eid: str) -> list[dict[str, Any]]:
        """返回从 eid 出发的所有邻接（含边信息 + 对端实体概要）。"""
        results: list[dict[str, Any]] = []
        for edge in self._edges.get(eid, []):
            dst_info = self._entities.get(edge["dst"], {})
            results.append({
                "eid": edge["dst"],
                "etype": dst_info.get("etype", "unknown"),
                "props": dst_info.get("props", {}),
                "edge_type": edge["etype"],
                "confidence": edge["confidence"],
            })
        return results

    async def close(self) -> None:
        """no-op。"""

    # ---- 测试/调试用辅助（不在抽象接口内） ----

    def get_entity(self, eid: str) -> dict[str, Any] | None:
        return self._entities.get(eid)

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self._evidences.get(evidence_id)

    def has_edge(self, src: str, dst: str, etype: str) -> bool:
        """检查是否存在指定类型的边。"""
        return any(
            e["dst"] == dst and e["etype"] == etype
            for e in self._edges.get(src, [])
        )
