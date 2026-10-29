"""文件资产库（六库之文件资产型）：prompts 片段 + skills 模块。

出厂资产随包分发（``secpt/assets/prompts/*.md``、``secpt/assets/skills/*.md``），
运维可用 ``SECPT_ASSETS_DIR`` 覆盖；引用方向 角色 ← skills ← prompts。
"""

from secpt.assets.library import (
    Skill,
    asset_roots,
    list_prompts,
    list_skills,
    load_skill,
    prompt_text,
    render_prompt,
    skill_fragments,
)

__all__ = [
    "Skill",
    "asset_roots",
    "list_prompts",
    "list_skills",
    "load_skill",
    "prompt_text",
    "render_prompt",
    "skill_fragments",
]
