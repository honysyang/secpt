"""评测报告：扩展 token / 时间 / 可观测指标。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TokenMetrics(BaseModel):
    """Token 消耗统计。"""

    total_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    by_model: dict[str, dict] = Field(default_factory=dict)


class TimingMetrics(BaseModel):
    """时间统计。"""

    duration_s: float = 0.0
    avg_wavefront_s: float = 0.0
    max_wavefront_s: float = 0.0
    min_wavefront_s: float = 0.0
    llm_total_s: float = 0.0
    tool_total_s: float = 0.0
    overhead_s: float = 0.0


class ObservabilityMetrics(BaseModel):
    """可观测性指标。"""

    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    format_errors: int = 0
    dedup_hits: int = 0
    nodes_spawned: int = 0
    nodes_dead: int = 0
    nodes_degraded: int = 0
    wavefronts: int = 0


class EvaluationScore(BaseModel):
    """评分卡。"""

    success: bool = False
    flag_found: bool = False
    flag_value: str = ""
    score: float = 0.0
    grade: str = "F"  # A / B / C / D / F
    breakdown: dict[str, float] = Field(default_factory=dict)


class DebugInfo(BaseModel):
    """调试信息。"""

    run_id: str = ""
    log_file: str = ""
    report_file: str = ""
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class WavefrontSnapshot(BaseModel):
    """一个波前的快照。"""

    index: int
    active_node_ids: list[str]
    dead_ids: list[str]
    degraded_ids: list[str]
    excluded_patterns: list[str]
    budget_spent: float
    duration_s: float = 0.0


class NodeFinalState(BaseModel):
    """Node 终态。"""

    id: str
    goal: str
    depth: int
    budget_remaining: float
    parent_id: str | None
    status: str
    reason: str
    evidence_ids: list[str]
    children: list[str]


class GraphStats(BaseModel):
    """每拍的图累计统计。"""

    wavefront: int
    entities: int
    edges: int
    evidences: int


class TrajectoryEntry(BaseModel):
    """一个 Node 的一次六拍记录。"""

    wavefront: int
    thought: str
    action: str
    observation: str
    duration_s: float = 0.0


class EngagementReport(BaseModel):
    """完整 Engagement 报告（含评测）。"""

    run_id: str = Field(default_factory=lambda: f"run.{uuid.uuid4().hex[:8]}")
    challenge_id: str = ""
    genome_summary: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: datetime | None = None
    duration_s: float = 0.0

    # 观测数据
    wavefronts: list[WavefrontSnapshot] = Field(default_factory=list)
    node_tree: list[NodeFinalState] = Field(default_factory=list)
    graph_stats: list[GraphStats] = Field(default_factory=list)
    trajectory: dict[str, list[TrajectoryEntry]] = Field(default_factory=dict)

    # §9.2 六项指标
    metrics: dict[str, Any] = Field(default_factory=dict)

    # 新增：token / 时间 / 可观测
    token_metrics: TokenMetrics = Field(default_factory=TokenMetrics)
    timing: TimingMetrics = Field(default_factory=TimingMetrics)
    observability: ObservabilityMetrics = Field(default_factory=ObservabilityMetrics)

    # 评测
    evaluation: EvaluationScore = Field(default_factory=EvaluationScore)
    debug: DebugInfo = Field(default_factory=DebugInfo)

    @property
    def total_wavefronts(self) -> int:
        return len(self.wavefronts)

    def summary(self) -> dict:
        """人类可读摘要。"""
        return {
            "run_id": self.run_id,
            "challenge_id": self.challenge_id,
            "duration_s": round(self.duration_s, 2),
            "wavefronts": self.total_wavefronts,
            "nodes": len(self.node_tree),
            "token": {
                "calls": self.token_metrics.total_calls,
                "tokens": self.token_metrics.total_tokens,
                "cost_usd": round(self.token_metrics.estimated_cost_usd, 4),
            },
            "evaluation": {
                "success": self.evaluation.success,
                "flag_found": self.evaluation.flag_found,
                "score": self.evaluation.score,
                "grade": self.evaluation.grade,
            },
        }


__all__ = [
    "DebugInfo",
    "EngagementReport",
    "EvaluationScore",
    "GraphStats",
    "NodeFinalState",
    "ObservabilityMetrics",
    "TimingMetrics",
    "TokenMetrics",
    "TrajectoryEntry",
    "WavefrontSnapshot",
]
