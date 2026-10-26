"""角色加载（WORKFLOW §7 M1：数据驱动角色库，assets/roles.py）。

角色是带 front matter 的 markdown，格式与 skills 完全同构—— differentiate
只在 front matter 键上：
    ---
    id: recon
    applies_to: [recon]        # 角色常用分工认领的任务类型（目录用，可空）
    summary: 一句话说明这个角色的立场与边界
    ---
    # 角色手册正文……

为什么不用代码里的枚举/映射表：角色随方法论演进而增删（新增渗透分工
不该动引擎代码），且 AssetSnapshot 冻结后不变——文件是天然的确定来源。

确定性（约束 B，与 skills 同规则）：按 id 排序返回，不依赖文件系统遍历
序；同 id 重复是配置错误，抛 PolicyViolation 而不是静默覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from pa.errors import PolicyViolation


@dataclass(frozen=True)
class Role:
    id: str
    task_types: list[str]  # 惯用分工（目录/提示用，不是硬约束）
    summary: str
    body: str  # 角色手册全文，任务带 role 时注入 worker


def _parse(path: Path) -> Role:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise PolicyViolation(f"角色缺少 front matter: {path.name}")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    rid = str(meta.get("id") or path.stem)
    applies = [str(x) for x in (meta.get("applies_to") or [])]
    return Role(
        id=rid,
        task_types=applies,
        summary=str(meta.get("summary") or ""),
        body=body.strip(),
    )


def load_roles(dirs) -> list[Role]:
    """从若干目录加载全部角色，按 id 排序（确定性，约束 B）。"""
    out: dict[str, Role] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            r = _parse(p)
            if r.id in out:
                raise PolicyViolation(f"角色 id 重复: {r.id} ({p})")
            out[r.id] = r
    return [out[k] for k in sorted(out)]
