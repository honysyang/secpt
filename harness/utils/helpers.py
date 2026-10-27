"""工具函数集合。"""

from __future__ import annotations

import re


def extract_steps(text: str) -> list[str]:
    """从文本中提取编号列表步骤。"""
    steps = []
    for line in text.split("\n"):
        line = line.strip()
        match = re.match(r"^[\d\-\*\•\)]+[\.\)]?\s*(.+)", line)
        if match:
            steps.append(match.group(1).strip())
    return steps if steps else [text.strip()]


def truncate(text: str, max_len: int = 200) -> str:
    """截断文本。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
