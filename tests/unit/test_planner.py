"""规划器单元测试。"""

import pytest

from harness.planner.base import PlanStep, Planner, TemplatePlanner


class FakeLLM:
    """模拟 LLM，返回 JSON 计划。"""

    def __init__(self, response: str):
        self.response = response
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.call_count += 1
        return {
            "choices": [{"message": {"content": self.response}}],
            "usage": {"total_tokens": 10},
        }


class TestPlanner:
    async def test_create_plan_without_llm(self):
        """无 LLM 时返回占位计划。"""
        planner = Planner()
        plan = await planner.create_plan("测试任务")

        assert plan.goal == "测试任务"
        assert len(plan.steps) == 1

    async def test_create_plan_with_json(self):
        """LLM 返回 JSON 时正确解析。"""
        llm = FakeLLM("""
        {"goal": "完成任务", "steps": [
            {"action": "步骤1", "tool": "tool1", "expected": "结果1"},
            {"action": "步骤2", "tool": null, "expected": "结果2"}
        ]}
        """)
        planner = Planner(llm)
        plan = await planner.create_plan("测试任务")

        assert plan.goal == "完成任务"
        assert len(plan.steps) == 2
        assert plan.steps[0].action == "步骤1"
        assert plan.steps[0].tool == "tool1"

    async def test_create_plan_with_markdown_json(self):
        """从 Markdown 代码块提取 JSON。"""
        llm = FakeLLM("""
        这是计划：
        ```json
        {"goal": "目标", "steps": [{"action": "步骤", "tool": null, "expected": ""}]}
        ```
        """)
        planner = Planner(llm)
        plan = await planner.create_plan("任务")

        assert plan.goal == "目标"
        assert len(plan.steps) == 1

    async def test_create_plan_fallback(self):
        """JSON 解析失败时回退到简单计划。"""
        llm = FakeLLM("这不是 JSON 格式")
        planner = Planner(llm)
        plan = await planner.create_plan("任务")

        assert plan.goal == "任务"
        assert len(plan.steps) == 1


class TestTemplatePlanner:
    def test_create_plan_with_template(self):
        """使用指定模板。"""
        planner = TemplatePlanner()
        plan = planner.create_plan("审查这段代码", template="code_review")

        assert plan.goal == "审查这段代码"
        assert len(plan.steps) == 4

    def test_create_plan_auto_match(self):
        """自动匹配关键词。"""
        planner = TemplatePlanner()
        plan = planner.create_plan("帮我做 data analysis")

        assert plan.goal == "帮我做 data analysis"
        assert len(plan.steps) == 5

    def test_create_plan_default(self):
        """无匹配时使用默认计划。"""
        planner = TemplatePlanner()
        plan = planner.create_plan("随机任务")

        assert plan.goal == "随机任务"
        assert len(plan.steps) == 1
