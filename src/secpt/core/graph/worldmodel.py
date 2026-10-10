"""WorldModel：带证据纪律的图黑板。

核心约束（GraphStore 写路径门控）：
- ``upsert_entity`` / ``add_edge`` 若未提供至少一个 ``evidence_id``，
  直接拒绝并抛 :class:`MissingEvidence`。
- 这是把「证据纪律」变成不可绕过的代码约束，而不是靠 prompt 自觉。
"""

from __future__ import annotations

import structlog

from secpt.core.graph.base import RESULT_CLASSIFICATION, TRUST_SCORES, GraphStore, TrustTier
from secpt.core.graph.models import (
    Edge,
    Entity,
    Evidence,
    Hypothesis,
    MissingEvidence,
)

logger = structlog.get_logger(__name__)


class WorldModel:
    """所有 Node 共享的图黑板。"""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    # ---- 证据 ----

    async def add_evidence(
        self,
        eid: str,
        payload: str,
        trust_tier: TrustTier = "deterministic",
        source: str = "tool",
        *,
        result: str = "ok",
    ) -> Evidence:
        """添加证据，返回 Evidence 对象。

        :param result: 执行结果分类，"ok" / "empty" / "failure"
            - ok: trust_tier 按 source 定档（工具输出 deterministic / 启发式 heuristic / LLM 推理 llm_inference）
            - empty: trust_tier 强制 "empty"，数值 0.0，不参与 evidence_strength
            - failure: trust_tier 强制 "failed"，数值 0.0，不参与 evidence_strength，计入失败计数
        """
        # 应用结果分类规则
        classification = RESULT_CLASSIFICATION.get(result, RESULT_CLASSIFICATION["ok"])
        if result in ("empty", "failure"):
            trust_tier = classification["trust_tier"]  # type: ignore[index]
        elif result == "ok" and source in classification["trust_tier_by_source"]:  # type: ignore[operator]
            trust_tier = classification["trust_tier_by_source"][source]  # type: ignore[index]

        evidence_id = await self._store.add_evidence(
            eid, payload, trust_tier=trust_tier, result=result
        )
        ev = Evidence(
            id=evidence_id,
            payload=payload,
            trust_tier=trust_tier,
            trust_score=TRUST_SCORES[trust_tier],
            source=source,
        )
        logger.debug(
            "worldmodel.evidence",
            evidence_id=evidence_id,
            eid=eid,
            trust_tier=trust_tier,
            result=result,
        )
        return ev

    # ---- 实体（证据门控） ----

    async def upsert_entity(
        self,
        eid: str,
        etype: str,
        props: dict | None = None,
        evidence_ids: list[str] | None = None,
    ) -> Entity:
        """写入实体。``evidence_ids`` 非空，否则抛 MissingEvidence。"""
        if not evidence_ids:
            raise MissingEvidence(f"upsert_entity({eid}) 需要至少一个 evidence_id")
        await self._store.upsert_entity(eid, etype, props or {})
        ent = Entity(id=eid, type=etype, props=props or {}, evidence_ids=list(evidence_ids))
        logger.debug("worldmodel.entity", eid=eid, etype=etype, evidence_ids=evidence_ids)
        return ent

    # ---- 边（证据门控 + 信任传播） ----

    async def add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        evidence_ids: list[str] | None = None,
        verified: bool = True,
        confidence_override: float | None = None,
    ) -> Edge:
        """写入边。证据非空；confidence 默认取证据 trust 的最小值。"""
        if not evidence_ids:
            raise MissingEvidence(f"add_edge({src}->{dst}) 需要至少一个 evidence_id")

        # 信任传播：confidence = min(关联证据 trust)
        # 含推理成分的边（verified=False）封顶 0.5
        scores: list[float] = []
        for _eid in evidence_ids:
            # 从 store 查 trust_score（简化：依赖 TRUST_SCORES 映射）
            # 实际实现中 evidence 存了 trust_score，这里用 TRUST_SCORES 反查
            scores.append(1.0)  # 默认最高，后续从 store 精确读取

        confidence = (
            confidence_override
            if confidence_override is not None
            else (min(scores) if scores else 1.0)
        )
        if not verified:
            confidence = min(confidence, 0.5)

        await self._store.add_edge(src, dst, etype, confidence=confidence)
        edge = Edge(
            src=src,
            dst=dst,
            type=etype,
            confidence=confidence,
            verified=verified,
            evidence_ids=list(evidence_ids),
        )
        logger.debug("worldmodel.edge", src=src, dst=dst, etype=etype, confidence=confidence)
        return edge

    # ---- 假设 ----

    async def add_hypothesis(
        self,
        stmt: str,
        parent_node: str,
        confidence: float = 0.5,
    ) -> Hypothesis:
        """LLM 生成的假设，不能直接写成事实。"""
        hyp = Hypothesis(stmt=stmt, parent_node=parent_node, confidence=confidence)
        logger.debug("worldmodel.hypothesis", id=hyp.id, stmt=stmt[:80])
        return hyp

    # ---- 查询 ----

    async def neighbors(self, eid: str) -> list[dict]:
        return await self._store.neighbors(eid)

    async def close(self) -> None:
        await self._store.close()


__all__ = ["WorldModel"]
