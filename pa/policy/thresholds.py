"""教练阈值（S12 约束 B）：全部走这里，代码里不得出现散落的魔法数字。

占位值，等真实数据回来再标定；改这里即改教练行为（验收五）。
"""

from __future__ import annotations

THRESHOLDS: dict[str, float] = {
    "thrashing_attempts": 3,  # max(task.attempt) >= 此值 → thrashing 信号
    "vs_rate": 0.3,  # vs_events / 已返回 worker 数 > 此值 → vs_rate 信号
    "authz_ratio": 0.5,  # AuthZ 失败 / 总失败 > 此值 → authz_ratio 信号
    "plan_budget_steps": 50,  # 计划任务 budget.steps > 此值 → I3
    "mission_task_cap": 24,  # 单 mission 任务总数上限（含 failed/blocked）
}
