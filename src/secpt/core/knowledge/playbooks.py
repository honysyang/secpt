"""playbook 加载器：从 YAML 加载领域先验。

YAML 结构：
    name: str          # playbook 名
    triggers: list     # 关键词正则
    goal: str          # 目标描述
    steps: list        # 有序候选动作，每步 {tool, args}
    pitfalls: list     # 常见坑
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Playbook:
    """单个 playbook。"""

    name: str = ""
    triggers: list[str] = field(default_factory=list)
    goal: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)


class PlaybookLibrary:
    """playbook 库：加载 + 匹配。"""

    def __init__(self, playbooks_dir: str | None = None) -> None:
        self._dir = playbooks_dir or str(Path(__file__).parent / "playbooks")
        self._playbooks: list[Playbook] = []

    def load(self) -> None:
        """从目录加载所有 YAML playbook。"""
        p = Path(self._dir)
        if not p.exists():
            return
        for yaml_file in sorted(p.glob("*.yaml")):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                pb = Playbook(
                    name=data.get("name", yaml_file.stem),
                    triggers=data.get("triggers", []),
                    goal=data.get("goal", ""),
                    steps=data.get("steps", []),
                    pitfalls=data.get("pitfalls", []),
                )
                self._playbooks.append(pb)
            except Exception:
                continue

    def load_default(self) -> None:
        """加载内置 playbook（与代码同目录的 playbooks/）。"""
        self._dir = str(Path(__file__).parent / "playbooks")
        self.load()

    def match(self, *, objective: str, observed: str = "") -> list[Playbook]:
        """按 objective + observed 文本匹配 triggers，返回命中的 playbook 列表。"""
        text = f"{objective}\n{observed}".lower()
        matched: list[Playbook] = []
        for pb in self._playbooks:
            for trigger in pb.triggers:
                try:
                    if re.search(trigger, text, re.IGNORECASE):
                        matched.append(pb)
                        break
                except re.error:
                    continue
        return matched

    def all(self) -> list[Playbook]:
        return list(self._playbooks)

    def __len__(self) -> int:
        return len(self._playbooks)


__all__ = ["Playbook", "PlaybookLibrary"]
