"""Profile：把先验知识（priors）渲染成 system_extra。

profile 名对应 knowledge/priors/<name>.md。system_extra 由骨架模板渲染：

    你是{角色}。

    【任务全文】
    {objective}

    {prior}

    【可用工具】
    {tools_doc}

    【约束】
    {constraints}

scope/目标地址约束属于 governance 层职责，不在这里写死。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PRIORS_DIR = Path(__file__).resolve().parent / "knowledge" / "priors"

DEFAULT_PROFILE = "ctf"

DEFAULT_ROLE = "渗透测试专家，正在解一道 CTF"

DEFAULT_PRIOR = "（无先验，按题面自行分析）"

DEFAULT_CONSTRAINTS = "不要扫描题面外的网段。"


@lru_cache(maxsize=16)
def load_prior(name: str) -> str:
    """加载 knowledge/priors/<name>.md 的全文。"""
    path = _PRIORS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"profile '{name}' 不存在: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_system_extra(
    profile: str = DEFAULT_PROFILE,
    *,
    objective: str,
    tools_doc: str,
    role: str = DEFAULT_ROLE,
    constraints: str = DEFAULT_CONSTRAINTS,
    prior: str | None = None,
) -> str:
    """按骨架模板渲染 system_extra（system prompt 中除任务全文外的部分）。"""
    if prior is None:
        prior = load_prior(profile)
    return f"""你是{role}。

【任务全文】
{objective}

{prior}

【可用工具】
{tools_doc}

【约束】
{constraints}"""
