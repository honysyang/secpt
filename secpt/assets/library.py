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
from collections.abc import Sequence
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
    tools: tuple[str, ...] = ()
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


class KnowledgeDoc(BaseModel):
    """知识库条目（设计文档 L43）：方法论/查询手册，只读参考。

    与 skills 的区别：skill 是"这单按什么动作序列做"（被角色/工单挂载），
    knowledge 是"这类问题一般怎么想"（按关键词命中出现在注意力包里，不改变
    任何执行路径）——前者是打法，后者是背景读物。
    """

    id: str
    title: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    body: str = ""


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
        tools=_str_tuple(meta.get("tools")),
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


def _parse_knowledge(path: Path) -> KnowledgeDoc:
    """解析知识条目：YAML 头（id/title/keywords）+ 正文（方法论）。"""
    meta, body = _load_document(path)
    return KnowledgeDoc(
        id=str(meta.get("id") or path.stem),
        title=str(meta.get("title") or path.stem),
        description=str(meta.get("description") or ""),
        keywords=_str_tuple(meta.get("keywords")),
        body=body,
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


def load_knowledge(doc_id: str) -> KnowledgeDoc | None:
    """按 id 读知识条目；不存在返回 None。"""
    path = _find("knowledge", doc_id)
    if path is None:
        return None
    return _parse_knowledge(path)


def list_knowledge() -> list[str]:
    """列知识条目 id。"""
    return _list_ids("knowledge")


def select_knowledge(text: str, *, limit: int = 2) -> list[KnowledgeDoc]:
    """按关键词命中挑知识条目（纯函数、零 LLM）：命中数降序、同分按 id 稳定。

    为什么是关键词而不是语义检索：注意力包必须是"同一输入同一输出"的可复算
    投影（重放/测试都依赖这一点）；语义相似度是 LLM 的活，放进打包路径会让
    每一步的输入不可复现。关键词够用——知识库的读者是主 LLM，标题与关键词
    已经写清了"这条适用于哪类问题"。
    """
    haystack = text.casefold()
    if not haystack.strip():
        return []
    scored: list[tuple[int, str, KnowledgeDoc]] = []
    for doc_id in list_knowledge():
        doc = load_knowledge(doc_id)
        if doc is None:
            continue
        score = sum(1 for keyword in doc.keywords if keyword.casefold() in haystack)
        if score > 0:
            scored.append((score, doc.id, doc))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [doc for _, _, doc in scored[:limit]]


def render_knowledge(docs: Sequence[KnowledgeDoc], *, max_chars: int = 600) -> list[str]:
    """把知识条目压成注意力包里的短行：标题 + 正文截断（防撑爆上下文）。"""
    lines: list[str] = []
    for doc in docs:
        body = doc.body.strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "…"
        lines.append(f"[{doc.title}] {body}")
    return lines


def role_catalog() -> list[str]:
    """渲染"可派角色"清单（M5-16）：主 LLM 的 ``agent_run.persona`` 取值域。

    设计文档 L8 要求主 LLM"读取角色库"——没有这份清单，persona 只能是自由文本，
    主 LLM 不知道系统里到底有哪些角色可派。清单只带 id/名称/一句描述/技能名，
    不带 instruction 正文（那是派单后 worker 才需要的东西），防注意力包膨胀。
    """
    lines: list[str] = []
    for role_id in list_roles():
        role = load_role(role_id)
        if role is None:
            continue
        desc = f"：{role.description}" if role.description else ""
        skills = f"；技能 {','.join(role.skills)}" if role.skills else ""
        lines.append(f"{role.id}（{role.name}）{desc}{skills}")
    return lines


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
    "KnowledgeDoc",
    "Role",
    "Skill",
    "asset_roots",
    "list_knowledge",
    "list_prompts",
    "list_roles",
    "list_skills",
    "load_knowledge",
    "load_role",
    "load_skill",
    "prompt_text",
    "render_knowledge",
    "render_prompt",
    "role_catalog",
    "role_fragments",
    "select_knowledge",
    "skill_fragments",
]
