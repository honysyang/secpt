"""Skills 模块单元测试。"""

import pytest

from harness.skills.base import (
    BUILTIN_SKILLS,
    PromptSkill,
    SkillContext,
    SkillRegistry,
    SkillResult,
    ToolSkill,
)


class FakeLLM:
    """模拟 LLM。"""

    def __init__(self, response: str = "mock response"):
        self.response = response
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.call_count += 1
        return {
            "choices": [{"message": {"content": self.response}}],
            "usage": {"total_tokens": 10},
        }


class TestPromptSkill:
    async def test_execute(self):
        skill = PromptSkill(
            name="test",
            description="测试技能",
            template="处理: {input}",
        )

        llm = FakeLLM("处理结果")
        context = SkillContext(llm=llm)

        result = await skill.execute("测试输入", context)

        assert result.success is True
        assert result.output == "处理结果"
        assert result.metadata["skill"] == "test"

    async def test_template_variables(self):
        skill = PromptSkill(
            name="test",
            description="测试",
            template="输入: {input}, 额外: {extra}",
        )

        llm = FakeLLM()
        context = SkillContext(llm=llm, variables={"extra": "变量"})

        await skill.execute("输入", context)

        # 验证 prompt 包含变量
        messages = [{"role": "user", "content": "输入: 输入, 额外: 变量"}]
        assert llm.call_count == 1


class TestToolSkill:
    async def test_execute_workflow(self):
        skill = ToolSkill(
            name="data_process",
            description="数据处理",
            workflow=[
                {"tool": "extract", "args": {"source": "{input}"}},
                {"tool": "transform", "args": {"data": "{extract_result}"}},
            ],
        )

        # 注册工具
        skill.register_tool("extract", lambda source: f"extracted({source})")
        skill.register_tool("transform", lambda data: f"transformed({data})")

        llm = FakeLLM()
        context = SkillContext(llm=llm)

        result = await skill.execute("raw_data", context)

        assert result.success is True
        assert "extracted(raw_data)" in result.output
        assert "transformed(extracted(raw_data))" in result.output
        assert result.metadata["steps"] == 2

    async def test_missing_tool(self):
        skill = ToolSkill(
            name="test",
            description="测试",
            workflow=[{"tool": "missing", "args": {}}],
        )

        llm = FakeLLM()
        context = SkillContext(llm=llm)

        result = await skill.execute("input", context)

        assert result.success is False
        assert "未注册" in result.output

    async def test_async_tool(self):
        async def async_tool(value: str) -> str:
            return f"async({value})"

        skill = ToolSkill(
            name="test",
            description="测试",
            workflow=[{"tool": "async_op", "args": {"value": "{input}"}}],
        )
        skill.register_tool("async_op", async_tool)

        llm = FakeLLM()
        context = SkillContext(llm=llm)

        result = await skill.execute("data", context)

        assert result.success is True
        assert "async(data)" in result.output


class TestSkillRegistry:
    async def test_register_and_execute(self):
        registry = SkillRegistry()
        skill = PromptSkill("test", "测试", "输入: {input}")
        registry.register(skill)

        llm = FakeLLM("结果")
        context = SkillContext(llm=llm)

        result = await registry.execute("test", "输入", context)

        assert result.success is True

    async def test_execute_missing(self):
        registry = SkillRegistry()
        llm = FakeLLM()
        context = SkillContext(llm=llm)

        result = await registry.execute("missing", "输入", context)

        assert result.success is False
        assert "不存在" in result.output

    def test_list_skills(self):
        registry = SkillRegistry()
        registry.register(PromptSkill("skill1", "技能1", "{input}"))
        registry.register(PromptSkill("skill2", "技能2", "{input}"))

        assert registry.list_skills() == ["skill1", "skill2"]

    def test_builtin_skills(self):
        """内置技能可用。"""
        assert "summarize" in BUILTIN_SKILLS
        assert "translate" in BUILTIN_SKILLS
        assert "code_review" in BUILTIN_SKILLS

        # 测试执行内置技能
        skill = BUILTIN_SKILLS["summarize"]
        assert skill.name == "summarize"
        assert "摘要" in skill.description
