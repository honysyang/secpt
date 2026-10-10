"""经验存储：var/knowledge/experiences.jsonl（append-only，sha1 去重）。

条目结构（在 knowledge/experience.py 的 Experience 基础上扩展）：
    id, title, technique, indicators, what_worked, pitfalls, outcome,
    task_type, job_id, ts
检索打分复用 ExperienceStore.search 的关键词机制。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("SECPT_VAR", "var")) / "knowledge" / "experiences.jsonl"


@dataclass
class LearnedExperience:
    """一次运行沉淀出的经验。"""

    title: str
    technique: str = ""  # 技术分类（如 saml_xsw / sql_injection）
    indicators: list[str] = field(default_factory=list)  # 匹配关键词
    what_worked: str = ""  # 行之有效的做法
    pitfalls: list[str] = field(default_factory=list)  # 踩过的坑/无效尝试
    outcome: str = "partial"  # success | fail | partial
    task_type: str = "generic"
    job_id: str = ""
    id: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "technique": self.technique,
            "indicators": self.indicators,
            "what_worked": self.what_worked,
            "pitfalls": self.pitfalls,
            "outcome": self.outcome,
            "task_type": self.task_type,
            "job_id": self.job_id,
            "ts": self.ts,
        }


def _fingerprint(exp: LearnedExperience) -> str:
    """去重指纹：技术 + 做法 + 结果（同法同果只记一次，避免题库刷分式膨胀）。"""
    raw = f"{exp.technique}|{exp.what_worked[:200]}|{exp.outcome}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class ExperienceJournal:
    """append-only 经验日志。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH

    def load(self) -> list[LearnedExperience]:
        if not self.path.is_file():
            return []
        out: list[LearnedExperience] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(LearnedExperience(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        return out

    def append(self, exp: LearnedExperience) -> bool:
        """追加一条经验；指纹已存在则跳过。返回是否写入。"""
        exp.id = exp.id or f"exp-{_fingerprint(exp)}"
        exp.ts = exp.ts or time.time()
        for old in self.load():
            if old.id == exp.id:
                return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(exp.to_dict(), ensure_ascii=False) + "\n")
        return True
