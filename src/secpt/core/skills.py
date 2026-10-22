"""技能动态发现（对齐 ZCode skills 机制）：运行时挂载的能力包。

一个技能 = 一个目录 + SKILL.md（frontmatter: name/description/when_to_use + 正文）。
发现路径（后者优先，重名覆盖前者）：
  1. 内置仓库 skills/                  （随版本发布）
  2. $SECPT_SKILLS_DIRS（: 或 ; 分隔）  （团队共享能力库）
  3. <var>/skills/                     （用户私有，可运行时增删即生效）

与 priors 的区别：priors 是"经验文档"（注入 system prompt 的打法记忆）；
技能是"结构化能力包"——指挥官从目录挑选后按任务赋予操作员（intents.skills 列），
执行体编译目标时把技能正文注入【技能包】节。新增能力不改代码、不重发布。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str  # 一句话是什么
    when_to_use: str  # 何时选我（指挥官选择依据，同 profile.scenario 语义）
    body: str  # SKILL.md 正文（frontmatter 之后，注入操作员目标）
    path: Path


def _repo_skills_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "skills"


def skills_dirs() -> list[Path]:
    """发现路径（低优先级在前；同名高优先级覆盖）。"""
    out = [_repo_skills_dir()]
    extra = os.environ.get("SECPT_SKILLS_DIRS", "")
    for part in re.split(r"[:;]", extra):
        p = part.strip()
        if p:
            out.append(Path(p).expanduser())
    out.append(Path(os.environ.get("SECPT_VAR", "var")) / "skills")
    return out


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_skill(path: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name, desc, when = path.parent.name, "", ""
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        for ln in m.group(1).splitlines():
            k, _, v = ln.partition(":")
            k, v = k.strip(), v.strip()
            if k == "name" and v:
                name = v
            elif k in ("description", "desc") and v:
                desc = v
            elif k in ("when_to_use", "when") and v:
                when = v
        body = text[m.end() :]
    else:
        # 无 frontmatter：首个标题行作 name，其余尽力而为
        for ln in text.splitlines():
            t = ln.strip().lstrip("#").strip()
            if t:
                name = t[:60]
                break
    if not name:
        return None
    return Skill(
        name=name,
        description=desc[:200],
        when_to_use=when[:300],
        body=body.strip()[:4000],
        path=path,
    )


def discover_skills() -> list[Skill]:
    """扫描全部发现路径，去重（高优先级覆盖同名）。"""
    found: dict[str, Skill] = {}
    for d in skills_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*/SKILL.md")):
            sk = _parse_skill(f)
            if sk is not None:
                found[sk.name] = sk  # 后写覆盖：路径列表越靠后优先级越高
    return sorted(found.values(), key=lambda s: s.name)


def valid_skill(name: str) -> bool:
    n = (name or "").strip()
    if not n or "/" in n or ".." in n:
        return False
    return any((d / n / "SKILL.md").is_file() for d in reversed(skills_dirs()))


def load_skill(name: str) -> Skill | None:
    n = (name or "").strip()
    if not valid_skill(n):
        return None
    for d in reversed(skills_dirs()):
        p = d / n / "SKILL.md"
        if p.is_file():
            return _parse_skill(p)
    return None
