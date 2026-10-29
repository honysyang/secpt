"""文件资产库（六库之文件资产型）：prompts / skills / 角色 / 知识。

出厂资产随包分发（``secpt/assets/{prompts,skills,roles,knowledge}``），运维可用
``SECPT_ASSETS_DIR`` 覆盖；引用方向 角色 ← skills ← prompts，知识库是只读参考。
"""

from secpt.assets.library import (
    KnowledgeDoc,
    Role,
    Skill,
    asset_roots,
    list_knowledge,
    list_prompts,
    list_roles,
    list_skills,
    load_knowledge,
    load_role,
    load_skill,
    prompt_text,
    render_knowledge,
    render_prompt,
    role_catalog,
    role_fragments,
    select_knowledge,
    skill_fragments,
)

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
