"""执行器：按步骤调用工具或子智能体。"""

from __future__ import annotations

from typing import Any

from harness.planner.base import Plan
from runtime.loop import ExecutionLoop, LoopResult


class Executor:
    """计划执行器。

    支持两种执行模式：
    - ReAct: 边想边做，灵活但可能绕路
    - Plan-and-Execute: 先规划再执行，结构清晰
    """

    def __init__(
        self,
        loop: ExecutionLoop | None = None,
        mode: str = "react",
    ) -> None:
        self.loop = loop
        self.mode = mode

    async def execute(self, plan: Plan, context: str = "") -> dict[str, Any]:
        """执行计划，返回结果字典。

        Args:
            plan: 执行计划
            context: 额外上下文

        Returns:
            包含执行结果和元数据的字典
        """
        if self.loop is None:
            # 无 LLM 时的占位实现
            return {
                "status": "placeholder",
                "goal": plan.goal,
                "steps": [s.action for s in plan.steps],
            }

        if self.mode == "plan_execute":
            result: LoopResult = await self.loop.run_plan_execute(plan, context)
        else:
            result = await self.loop.run_react(plan.goal, context)

        return {
            "status": "completed",
            "goal": plan.goal,
            "output": result.output,
            "steps": result.steps,
            "total_tokens": result.total_tokens,
        }

    async def execute_react(self, task: str, context: str = "") -> dict[str, Any]:
        """ReAct 模式执行单个任务。"""
        if self.loop is None:
            return {"status": "placeholder", "task": task}

        result = await self.loop.run_react(task, context)
        return {
            "status": "completed",
            "output": result.output,
            "steps": result.steps,
            "total_tokens": result.total_tokens,
        }
