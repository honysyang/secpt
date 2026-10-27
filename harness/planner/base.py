"""规划器：拆解任务为可执行步骤。

支持两种模式：
1. LLM 规划：调用 LLM 生成结构化计划
2. 模板规划：基于预定义模板快速匹配
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class PlanStep:
    """计划中的一个步骤。"""

    action: str
    tool: str | None = None
    expected: str = ""


@dataclass
class Plan:
    """完整计划。"""

    goal: str
    steps: list[PlanStep]


class LLMClient(Protocol):
    """LLM 客户端协议。"""

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        """发送聊天请求。"""
        ...


class Planner:
    """任务规划器。

    支持：
    - LLM 生成计划
    - 解析结构化输出
    - 步骤依赖管理
    """

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm

    async def create_plan(self, task: str, context: str = "") -> Plan:
        """根据任务描述生成计划。

        Args:
            task: 任务描述
            context: 额外上下文

        Returns:
            Plan 包含目标步骤列表
        """
        if self.llm is None:
            # 无 LLM 时的占位实现
            return Plan(
                goal=task,
                steps=[PlanStep(action="分析任务", tool=None, expected="理解需求")],
            )

        # 调用 LLM 生成计划
        messages = [
            {
                "role": "system",
                "content": (
                    "你是任务规划专家。将用户任务拆解为可执行步骤。\n"
                    "输出格式（JSON）:\n"
                    '{"goal": "任务目标", "steps": [{"action": "步骤描述", "tool": "工具名或null", "expected": "预期结果"}]}\n'
                    "最多 5 步，每步不超过 30 字。"
                ),
            },
            {"role": "user", "content": f"任务: {task}\n上下文: {context}"},
        ]

        response = await self.llm.chat(messages)
        content = response["choices"][0]["message"].get("content", "")

        # 解析 JSON
        plan_data = self._extract_json(content)
        if plan_data:
            return Plan(
                goal=plan_data.get("goal", task),
                steps=[
                    PlanStep(
                        action=s.get("action", ""),
                        tool=s.get("tool"),
                        expected=s.get("expected", ""),
                    )
                    for s in plan_data.get("steps", [])
                ],
            )

        # 解析失败，回退到简单计划
        return Plan(
            goal=task,
            steps=[PlanStep(action=task, tool=None, expected="完成任务")],
        )

    def _extract_json(self, text: str) -> dict[str, Any] | None:
        """从文本中提取 JSON。"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试从 Markdown 代码块提取
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试查找 JSON 对象
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return None


class TemplatePlanner:
    """模板规划器：基于预定义模板快速生成计划。"""

    TEMPLATES = {
        "code_review": Plan(
            goal="代码审查",
            steps=[
                PlanStep(action="分析代码结构", tool=None, expected="理解代码组织"),
                PlanStep(action="检查潜在 bug", tool="static_analysis", expected="发现问题"),
                PlanStep(action="评估代码风格", tool=None, expected="风格建议"),
                PlanStep(action="生成审查报告", tool=None, expected="输出报告"),
            ],
        ),
        "data_analysis": Plan(
            goal="数据分析",
            steps=[
                PlanStep(action="加载数据", tool="load_data", expected="数据就绪"),
                PlanStep(action="数据清洗", tool="clean_data", expected="清洗完成"),
                PlanStep(action="统计分析", tool="analyze", expected="统计结果"),
                PlanStep(action="可视化", tool="visualize", expected="生成图表"),
                PlanStep(action="生成报告", tool=None, expected="分析报告"),
            ],
        ),
    }

    def create_plan(self, task: str, template: str | None = None) -> Plan:
        """基于模板生成计划。

        Args:
            task: 任务描述（用于替换 goal）
            template: 模板名称，不指定则自动匹配

        Returns:
            Plan 基于模板生成
        """
        if template and template in self.TEMPLATES:
            plan = self.TEMPLATES[template]
            return Plan(goal=task, steps=plan.steps)

        # 自动匹配关键词
        task_lower = task.lower()
        for name, plan in self.TEMPLATES.items():
            if any(kw in task_lower for kw in name.split("_")):
                return Plan(goal=task, steps=plan.steps)

        # 默认计划
        return Plan(
            goal=task,
            steps=[PlanStep(action=task, tool=None, expected="完成任务")],
        )
