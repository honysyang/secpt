"""WorldModel 图黑板 Schema。

证据纪律：图上任何结论必须挂载 ``evidence_id``。
没有证据，不得写入实体/边/结论。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from secpt.core.graph.base import TrustTier


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _gen_id(prefix: str) -> str:
    return f"{prefix}.{uuid.uuid4().hex[:12]}"


# ---- 模型 ----


class Entity(BaseModel):
    """图实体：主机/域名/端口/服务/组件/凭证/漏洞……"""

    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    props: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class Edge(BaseModel):
    """有向边。confidence = min(关联证据 trust)；推理边封顶 0.5 且 verified=False。"""

    model_config = ConfigDict(frozen=True)

    src: str
    dst: str
    type: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    verified: bool = True
    evidence_ids: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    """一切事实的原子来源。"""

    model_config = ConfigDict(frozen=True)

    id: str
    payload: str
    trust_tier: TrustTier
    trust_score: float = Field(ge=0.0, le=1.0)
    source: str = "tool"
    ts: str = Field(default_factory=_utcnow)


class HypothesisStatus(str, Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    SUPERSEDED = "superseded"


class Hypothesis(BaseModel):
    """未经证实的 LLM 产物，不能直接降级成事实。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: _gen_id("hyp"))
    stmt: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    parent_node: str
    ts: str = Field(default_factory=_utcnow)


class DeathReason(str, Enum):
    NO_LIVE_TARGETS = "no_live_targets"
    TOOL_FAILED_PERMANENTLY = "tool_failed_permanently"
    HYPOTHESIS_REFUTED = "hypothesis_refuted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SUPERSEDED_BY_SIBLING = "superseded_by_sibling"


class DeathCertificate(BaseModel):
    """机制①产出，写入全局排除清单。"""

    model_config = ConfigDict(frozen=True)

    node_id: str
    reason: DeathReason
    evidence_ids: list[str] = Field(default_factory=list)
    ts: str = Field(default_factory=_utcnow)


class InFlightAction(BaseModel):
    """推测执行追踪与回滚依据。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: _gen_id("inflight"))
    node_id: str
    tool: str
    args: str
    checkpoint_id: str | None = None
    ts: str = Field(default_factory=_utcnow)


# ---- 异常 ----


class MissingEvidence(Exception):
    """写入实体/边时未提供 evidence_id，违反证据纪律。"""


# ---- NarrowingSignals ----


@dataclass
class NarrowingSignals:
    """面收缩三机制的系统级信号，Scheduler 每轮开始前读取。"""

    excluding: list[DeathCertificate] = field(default_factory=list)
    wakeable: list[str] = field(default_factory=list)  # 持有关键证据的节点 id
    degraded: list[str] = field(default_factory=list)  # 被机制③降级的节点 id


__all__ = [
    "DeathCertificate",
    "DeathReason",
    "Edge",
    "Entity",
    "Evidence",
    "Hypothesis",
    "HypothesisStatus",
    "InFlightAction",
    "MissingEvidence",
    "NarrowingSignals",
]
