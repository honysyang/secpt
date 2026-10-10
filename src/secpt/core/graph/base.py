"""GraphStore 抽象。

信任等级：
- ``deterministic``  (1.0)：工具直接输出，最高可信
- ``heuristic``      (0.8)：启发式推断
- ``llm_inference``  (0.5)：LLM 生成，需要复核
- ``empty``          (0.0)：工具返回空结果，不参与 evidence_strength
- ``failed``         (0.0)：工具执行失败，不参与 evidence_strength，计入失败计数
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TrustTier = Literal["deterministic", "heuristic", "llm_inference", "empty", "failed"]

TRUST_SCORES: dict[str, float] = {
    "deterministic": 1.0,
    "heuristic": 0.8,
    "llm_inference": 0.5,
    "empty": 0.0,
    "failed": 0.0,
}

# ---- 结果分类规则（常量表，不散落 if） ----

# result 分类 → (trust_tier, 是否参与 evidence_strength, 是否计入失败计数)
RESULT_CLASSIFICATION: dict[str, dict[str, Any]] = {
    "ok": {
        "trust_tier_by_source": {
            "tool": "deterministic",
            "heuristic": "heuristic",
            "llm": "llm_inference",
        },
        "participates_in_strength": True,
        "counts_as_failure": False,
    },
    "empty": {
        "trust_tier": "empty",
        "participates_in_strength": False,
        "counts_as_failure": False,
    },
    "failure": {
        "trust_tier": "failed",
        "participates_in_strength": False,
        "counts_as_failure": True,
    },
}


class EvidenceRecord(BaseModel):
    """一条证据记录。"""

    evidence_id: str
    eid: str
    payload: str
    trust_tier: TrustTier
    trust_score: float = Field(default=0.0, ge=0.0, le=1.0)

    model_config = ConfigDict(frozen=True)


class GraphStore(ABC):
    """图存储抽象基类。所有方法异步。"""

    @abstractmethod
    async def upsert_entity(self, eid: str, etype: str, props: dict | None = None) -> None:
        """插入或更新实体。"""

    @abstractmethod
    async def add_edge(
        self,
        src: str,
        dst: str,
        etype: str,
        confidence: float | None = None,
    ) -> None:
        """添加一条有向边。"""

    @abstractmethod
    async def add_evidence(
        self,
        eid: str,
        payload: str,
        trust_tier: TrustTier = "deterministic",
        *,
        result: str = "ok",
    ) -> str:
        """为实体添加证据，返回 evidence_id。

        :param result: 执行结果分类，"ok" / "empty" / "failure"
            failure/empty 的 trust_tier 会被强制设为 "failed"/"empty"（数值 0.0）
        """

    @abstractmethod
    async def neighbors(self, eid: str) -> list[dict[str, Any]]:
        """返回邻接实体列表（含边信息）。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭连接（内存实现为 no-op）。"""


__all__ = [
    "RESULT_CLASSIFICATION",
    "TRUST_SCORES",
    "EvidenceRecord",
    "GraphStore",
    "TrustTier",
]
