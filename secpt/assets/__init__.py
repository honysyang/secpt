"""文件资产库（六库之文件资产型）：prompts 片段 + skills 模块 + 角色包。

出厂资产随包分发（``secpt/assets/prompts/*.md``、``skills/*.md``、``roles/*.yaml``），
运维可用 ``SECPT_ASSETS_DIR`` 覆盖；引用方向 角色 ← skills ← prompts。
"""

from secpt.assets.library import (
    Role,
    Skill,
    asset_roots,
    list_prompts,
    list_roles,
    list_skills,
    load_role,
    load_skill,
    prompt_text,
    render_prompt,
    role_fragments,
    skill_fragments,
)

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
