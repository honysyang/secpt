"""技能加载（ARCHITECTURE §2.3：assets/skills.py）。

技能是带 front matter 的 markdown：
    ---
    id: port-scan
    applies_to: [recon]        # 空列表 = 全任务类型适用
    ---
    # 正文……

确定性：按 id 排序返回，不依赖文件系统遍历序；同 id 重复是配置错误，
抛 PolicyViolation 而不是静默覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from pa.errors import PolicyViolation


@dataclass(frozen=True)
class Skill:
    id: str
    task_types: list[str]
    trigger: str  # planner 目录行用：什么情形下适用
    summary: str  # planner 目录行用：一句摘要
    body: str  # 全文，只给 worker
    # 限定适配角色（空 = 任意角色都注入）。M1：任务带 role 时 worker 的
    # 技能正文按 (task_type, role) 双过滤——角色专属纪律不该喂给别的角色。
    roles: list[str] = field(default_factory=list)


def _parse(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise PolicyViolation(f"技能缺少 front matter: {path.name}")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    sid = str(meta.get("id") or path.stem)
    applies = [str(x) for x in (meta.get("applies_to") or [])]
    tagged = [str(x) for x in (meta.get("roles") or [])]
    return Skill(
        id=sid,
        task_types=applies,
        trigger=str(meta.get("trigger") or ""),
        summary=str(meta.get("summary") or ""),
        body=body.strip(),
        roles=tagged,
    )


def load_skills(dirs) -> list[Skill]:
    """从若干目录加载全部技能，按 id 排序（确定性，约束 B）。"""
    out: dict[str, Skill] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            s = _parse(p)
            if s.id in out:
                raise PolicyViolation(f"技能 id 重复: {s.id} ({p})")
            out[s.id] = s
    return [out[k] for k in sorted(out)]
