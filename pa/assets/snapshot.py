"""资产快照：mission 级冻结（ARCHITECTURE §2.3：assets/snapshot.py）。

freeze 之后本 mission 内不变——这是 system 段逐字节稳定（prefix cache
不破）的前提。skills 按 id 排序、工具按 name 排序，全部确定性。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pa.assets.roles import Role, load_roles
from pa.assets.skills import Skill, load_skills
from pa.errors import SecptError
from pa.tools import manifest


@dataclass(frozen=True)
class AssetSnapshot:
    prompts: dict[str, str]  # phase → 模板文本（不含时间戳/进度）
    skills: tuple[Skill, ...]  # 按 id 排序
    tools: tuple[dict, ...]  # manifest，按 name 排序
    roles: tuple[Role, ...] = ()  # 角色库（M1），按 id 排序；空 = 未配置

    def prompt(self, phase: str) -> str:
        try:
            return self.prompts[phase]
        except KeyError:
            raise SecptError(f"未知 phase: {phase}") from None

    def skills_for(self, task_type: str, role: str = "") -> list[Skill]:
        """适配某任务类型的技能，按 id 排序。applies_to 为空 = 全适用。

        M1 角色双过滤：角色专属技能（front matter 带 roles）只注入给
        归属角色——role 参数缺省 "" 时同样注入旧文件里无 roles 标签
        的技能，保证未配置角色库的既有行为逐字节不变。"""
        out = [
            s
            for s in self.skills
            if (not s.task_types or task_type in s.task_types)
            and (not s.roles or role in s.roles)
        ]
        return sorted(out, key=lambda s: s.id)

    def role_for(self, role_id: str) -> Role | None:
        """按 id 取角色；未配置/不存在返回 None（调用方静默降级）。"""
        if not role_id:
            return None
        for r in self.roles:
            if r.id == role_id:
                return r
        return None

    @classmethod
    def freeze(cls, prompt_dir, skill_dirs, role_dirs=()) -> "AssetSnapshot":
        prompt_dir = Path(prompt_dir)
        prompts = {
            p.stem: p.read_text(encoding="utf-8")
            for p in sorted(prompt_dir.glob("*.md"))
        }
        skills = load_skills(skill_dirs)
        tools = sorted(manifest(), key=lambda t: t["name"])
        return cls(
            prompts=prompts,
            skills=tuple(skills),
            tools=tuple(tools),
            roles=tuple(load_roles(role_dirs)),
        )
