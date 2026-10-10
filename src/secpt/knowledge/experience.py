"""Phase 3.3：策展知识库 / 经验库。

存储渗透测试经验条目，供机制③（经验匹配降级）检索使用。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Experience:
    """一条渗透经验。"""

    id: str
    title: str
    description: str
    technique: str  # 技术分类（如 "web_fingerprint", "port_scan"）
    indicators: list[str]  # 匹配指标关键词
    severity: str = "medium"
    trust_tier: str = "heuristic"
    ts: str = ""


class ExperienceStore:
    """经验库存储，JSON 文件持久化。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else None
        self._experiences: dict[str, Experience] = {}
        if self._path and self._path.exists():
            self._load()

    def add(self, exp: Experience) -> None:
        """添加经验。"""
        if not exp.id:
            exp.id = f"exp.{uuid.uuid4().hex[:12]}"
        self._experiences[exp.id] = exp
        logger.debug("experience.add", id=exp.id, title=exp.title[:50])

    def get(self, exp_id: str) -> Experience | None:
        return self._experiences.get(exp_id)

    def all(self) -> list[Experience]:
        return list(self._experiences.values())

    def search(self, query: str, limit: int = 5) -> list[Experience]:
        """按关键词搜索经验。"""
        query_lower = query.lower()
        scored: list[tuple[float, Experience]] = []
        for exp in self._experiences.values():
            score = 0.0
            # 标题和描述匹配
            if query_lower in exp.title.lower():
                score += 3.0
            if query_lower in exp.description.lower():
                score += 2.0
            # 指标匹配
            for ind in exp.indicators:
                if ind.lower() in query_lower or query_lower in ind.lower():
                    score += 1.5
            if score > 0:
                scored.append((score, exp))
        scored.sort(key=lambda x: -x[0])
        return [exp for _, exp in scored[:limit]]

    def match(self, text: str) -> list[Experience]:
        """匹配文本中的经验（用于机制③）。"""
        return self.search(text)

    def _load(self) -> None:
        """从 JSON 文件加载。"""
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                exp = Experience(**item)
                self._experiences[exp.id] = exp
            logger.info("experience.loaded", count=len(self._experiences))
        except Exception as exc:
            logger.error("experience.load_error", error=str(exc))

    def save(self) -> None:
        """保存到 JSON 文件。"""
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "id": e.id,
                "title": e.title,
                "description": e.description,
                "technique": e.technique,
                "indicators": e.indicators,
                "severity": e.severity,
                "trust_tier": e.trust_tier,
                "ts": e.ts,
            }
            for e in self._experiences.values()
        ]
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("experience.saved", count=len(data))


__all__ = ["Experience", "ExperienceStore"]
