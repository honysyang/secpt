"""编排模式唯一解析点（v10 模式矩阵收敛）。

行为分岔过去由一串开关笛卡尔积决定（ORCH_COMMANDER × ROLE_TEMPLATES × GOALS ×
stages × role_plan × benchmark），组合数随开关线性爆炸、测试矩阵不可能全覆盖。
这里把"当前任务以什么模式跑"收敛成唯一入口：所有开关在一个函数里归约为一个
显式模式枚举，非法/歧义组合打告警并取确定优先级——不再有隐式行为。

模式优先级（先到先定，后者打降级告警）：
    benchmark 闸口（dag 层已定，不再此处）> stages 清单 > role_plan 团队 legacy
    > commander（默认）> legacy（ORCH_COMMANDER=0，弃用告警）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class OrchMode:
    mode: str  # commander | stages | team-legacy | legacy
    warnings: list[str] = field(default_factory=list)

    def warn(self) -> str:
        return "；".join(self.warnings)


def resolve_orch_mode(
    *,
    stages: list | tuple | None = None,
    role_plan: bool = False,
    env: dict | None = None,
) -> OrchMode:
    """归约全部编排开关为一个显式模式；歧义组合在这里显式裁决并告警。"""
    e = os.environ if env is None else env
    w: list[str] = []
    if stages:
        if e.get("SECPT_ORCH_COMMANDER", "1") == "0":
            w.append(
                "stages 清单模式与 SECPT_ORCH_COMMANDER=0 冲突：清单模式优先（清单语义不依赖指挥官）"
            )
        return OrchMode("stages", w)
    if role_plan:
        if e.get("SECPT_ROLE_TEMPLATES", "0") == "1":
            w.append(
                "role_plan 与 ROLE_TEMPLATES 同为 legacy 团队路径：role_plan 优先（固定 DAG 骨架）"
            )
        else:
            w.append(
                "role_plan 团队模式为 legacy 路径（v5 起任务理解归指挥官）：建议改用 auto + 指挥官首轮理解"
            )
        return OrchMode("team-legacy", w)
    if e.get("SECPT_ROLE_TEMPLATES", "0") == "1":
        w.append("SECPT_ROLE_TEMPLATES=1 为 legacy 五角色路径（默认关闭）：建议迁移指挥官角色赋予")
        # 仍走 commander 主循环（legacy 模板只影响 planner 注入），但显式标记
        if e.get("SECPT_ORCH_COMMANDER", "1") == "0":
            return OrchMode(
                "legacy", [*w, "SECPT_ORCH_COMMANDER=0：v3 planner 模式已弃用，将在后续版本移除"]
            )
        return OrchMode("commander", w)
    if e.get("SECPT_ORCH_COMMANDER", "1") == "0":
        return OrchMode(
            "legacy", ["SECPT_ORCH_COMMANDER=0：v3 planner 模式已弃用，将在后续版本移除"]
        )
    return OrchMode("commander", w)
