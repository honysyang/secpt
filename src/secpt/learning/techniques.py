"""通用打法模板库（数据驱动，非跑分专属）。

来源：`secpt/knowledge/techniques.yaml`（技术信号 → 打法速查）。按意图 goal/标题/
资产做关键词匹配，命中时注入 objective 作为线索（最多 2 条、总长 ≤700 字），
不做任何执行或目标假设。

设计取舍：
- 纯数据，新增打法只改 YAML；
- 只在命中明确信号时注入（避免每条意图都烧 token）；
- SECPT_TECHNIQUES=0 可关闭；
- 与 learning/playbook.py 的分工：playbook 是"历史实战蒸馏"，这里是"通用先验"。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

_TECH_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "techniques.yaml"
_CACHE: list[dict] | None = None
_MAX_ITEMS = 2
_MAX_CHARS = 700


def _load() -> list[dict]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        data = yaml.safe_load(_TECH_PATH.read_text(encoding="utf-8")) or []
        _CACHE = [d for d in data if isinstance(d, dict) and d.get("name")]
    except Exception:
        _CACHE = []
    return _CACHE


def match_techniques(text: str, *, limit: int = _MAX_ITEMS) -> str:
    """按信号匹配打法模板，返回可注入 objective 的文本块（无命中返回空串）。"""
    if os.environ.get("SECPT_TECHNIQUES", "1") == "0":
        return ""
    low = (text or "").lower()
    if not low.strip():
        return ""
    scored: list[tuple[int, dict]] = []
    for item in _load():
        inds = [str(i).lower() for i in item.get("indicators") or [] if str(i).strip()]
        score = sum(1 for i in inds if i in low)
        if score > 0:
            scored.append((score, item))
    if not scored:
        return ""
    scored.sort(key=lambda x: -x[0])
    parts: list[str] = ["【通用打法参考（命中信号）】"]
    for _score, item in scored[: max(1, limit)]:
        parts.append(f"■ {item.get('name')}: {item.get('summary') or ''!s}")
        for step in (item.get("steps") or [])[:3]:
            parts.append(f"  - {step}")
    return "\n".join(parts)[:_MAX_CHARS]


__all__ = ["match_techniques"]
