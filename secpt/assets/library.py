"""文件资产库（六库之文件资产型）：prompts 片段 / skills 模块 / 角色包的只读装载。

为什么要有这一层（设计文档 L41-45 六库裁剪的直接后果）：
- 六库拍板把 prompts/skills/角色/知识归为"文件资产型（yaml/md，git 版本化）"。
  在此之前 prompt 内联在 Python 常量里——同一段话术散落在多个器官，改一处忘
  一处就是版本漂移，且话术评审要读代码。资产库把"话术"变成可 diff、可评审的
  文件，代码只保留"用哪个资产 + 填什么值"。
- 引用方向固定为 角色 ← skills ← prompts（上层打包下层，不反向重复内容）：
  角色用 ``skills`` 引用技能、用 ``prompts`` 引用纪律片段；技能用 ``prompts``
  引用纪律片段。装载时由 :func:`role_fragments` / :func:`skill_fragments` 展开
  成正文。任一侧缺件都抛 ``E_ASSET``——空话术会让 LLM 行为不可解释，
  静默降级成空字符串比报错更危险。

查找链（两道）：
1. ``SECPT_ASSETS_DIR``（运维覆盖目录，按 ``prompts/`` ``skills/`` ``roles/`` 分族）；
2. 包内 ``secpt/assets``（出厂资产，随 wheel 分发，离线可跑）。
两道都没有 → ``E_ASSET`` 拒跑，绝不返回空串。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from secpt.errors import SecptError

_PACKAGE_ROOT = Path(__file__).resolve().parent

# 资产后缀：md 写正文类资产，yaml 写带结构的角色包（id/skills/prompts）。
_SUFFIXES: tuple[str, ...] = (".md", ".yaml", ".yml")

# 模板占位符只认小写/下划线标识符：避免把 JSON 里的花括号当字段。
_TEMPLATE_FIELD = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class Skill(BaseModel):
    """技能模块：可复用动作序列（做法）+ 引用的 prompt 片段（纪律）。

    设计文档 L42：skills 库是"可复用动作模块（流程/命令序列），被角色引用"。
    """

    id: str
    name: str
    description: str = ""
    prompts: tuple[str, ...] = ()
    body: str = ""


class Role(BaseModel):
    """角色包：who + how 的打包单元（设计文档 L41）。

    ``skills`` 引用技能库、``prompts`` 内联纪律模板、``instruction`` 是本角色的
    行事风格——派单时整包挂给 worker，worker 不再自己猜"我该怎么干"。
    """

    id: str
    name: str
    description: str = ""
    skills: tuple[str, ...] = ()
    prompts: tuple[str, ...] = ()
    instruction: str = ""


def asset_roots() -> tuple[Path, ...]:
    """资产查找根：env 覆盖目录在前、包内出厂目录在后（前者胜出）。"""
    override = os.environ.get("SECPT_ASSETS_DIR", "").strip()
    roots: tuple[Path, ...] = (Path(override), _PACKAGE_ROOT) if override else (_PACKAGE_ROOT,)
    return roots


def _find(family: str, asset_id: str) -> Path | None:
    """按族（prompts/skills/roles）与 id 找文件；未命中返回 None（调用方决定降级口径）。"""
    for root in asset_roots():
        for suffix in _SUFFIXES:
            candidate = root / family / f"{asset_id}{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _missing(kind: str, asset_id: str) -> SecptError:
    return SecptError(
        f"{kind} 资产缺失（E_ASSET）",
        code="E_ASSET",
        context={"asset": asset_id, "roots": [str(r) for r in asset_roots()]},
    )


def prompt_text(asset_id: str) -> str:
    """读一段 prompt 原文（不做替换）。缺失即 E_ASSET——空话术不允许静默存在。"""
    path = _find("prompts", asset_id)
    if path is None:
        raise _missing("prompt", asset_id)
    return path.read_text(encoding="utf-8").strip()


def render_prompt(asset_id: str, **values: str) -> str:
    """按 ``{field}`` 占位替换后返回 prompt。

    未知占位符原样保留（可见而非被悄悄抹成空）：模板里写错字段名时，LLM 输入
    里能直接看到 ``{wrong_name}``，比静默丢值更容易被测试与人工发现。
    """

    def _sub(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return _TEMPLATE_FIELD.sub(_sub, prompt_text(asset_id))


def list_prompts() -> list[str]:
    """列资产 id（测试/巡检用：确认出厂资产在包内可枚举）。"""
    return _list_ids("prompts")


def _list_ids(family: str) -> list[str]:
    ids: set[str] = set()
    for root in asset_roots():
        directory = root / family
        if not directory.is_dir():
            continue
        for suffix in _SUFFIXES:
            ids.update(path.stem for path in directory.glob(f"*{suffix}"))
    return sorted(ids)


def _load_document(path: Path) -> tuple[dict[str, object], str]:
    """读资产文件：可选 YAML 头 + 正文。

    头缺失也能活（id 取文件名、正文整篇）：允许"先写做法、后补元数据"；
    只有元数据写坏（YAML 解析失败）才 E_ASSET——那是笔误，静默吞掉会掩盖真问题。
    """
    text = path.read_text(encoding="utf-8")
    meta: dict[str, object] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                raw = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as exc:
                raise SecptError(
                    "资产头解析失败（E_ASSET）",
                    code="E_ASSET",
                    context={"path": str(path), "reason": str(exc)},
                ) from exc
            if isinstance(raw, dict):
                meta = raw
            body = parts[2]
    return meta, body.strip()


def _str_tuple(raw: object) -> tuple[str, ...]:
    """YAML 里 scalar/清单两写都收（``prompts: x`` 与 ``prompts: [x, y]``）。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    return ()


def _parse_skill(path: Path) -> Skill:
    """解析技能文件（YAML 头 + 正文）。"""
    meta, body = _load_document(path)
    return Skill(
        id=str(meta.get("id") or path.stem),
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or ""),
        prompts=_str_tuple(meta.get("prompts")),
        body=body,
    )


def _parse_role(path: Path) -> Role:
    """解析角色包：``instruction`` 可写在头里（多行）或正文里（取正文）。"""
    meta, body = _load_document(path)
    instruction = str(meta.get("instruction") or "").strip() or body
    return Role(
        id=str(meta.get("id") or path.stem),
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or ""),
        skills=_str_tuple(meta.get("skills")),
        prompts=_str_tuple(meta.get("prompts")),
        instruction=instruction,
    )


def load_skill(skill_id: str) -> Skill | None:
    """按 id 读技能；不存在返回 None（工单挂载侧降级留痕，不静默假装挂上）。"""
    path = _find("skills", skill_id)
    if path is None:
        return None
    return _parse_skill(path)


def list_skills() -> list[str]:
    """列技能 id（装配/巡检用）。"""
    return _list_ids("skills")


def load_role(role_id: str) -> Role | None:
    """按 id 读角色包；不存在返回 None（persona 自由文本沿用，不阻断派单）。"""
    path = _find("roles", role_id)
    if path is None:
        return None
    return _parse_role(path)


def list_roles() -> list[str]:
    """列角色 id（装配/巡检用）。"""
    return _list_ids("roles")


def _fragments(prompt_ids: tuple[str, ...]) -> list[str]:
    """展开 prompt 引用（角色/技能共用的引用链实现点）。

    引用缺失抛 E_ASSET：资产声称挂在某条纪律上却找不到纪律原文，是资产库
    内部不一致——必须报出来，不能当成"没有纪律"。
    """
    return [prompt_text(prompt_id) for prompt_id in prompt_ids]


def skill_fragments(skill: Skill) -> list[str]:
    """展开 skill 引用的 prompt 片段（skills ← prompts）。"""
    return _fragments(skill.prompts)


def role_fragments(role: Role) -> list[str]:
    """展开角色引用的 prompt 片段（roles ← prompts）。"""
    return _fragments(role.prompts)


__all__ = [
    "Role",
    "Skill",
    "asset_roots",
    "list_prompts",
    "list_roles",
    "list_skills",
    "load_role",
    "load_skill",
    "prompt_text",
    "render_prompt",
    "role_fragments",
    "skill_fragments",
]
