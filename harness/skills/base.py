"""Skills 模块：可复用的智能体技能。

Skill 是智能体的"能力单元"，可以：
- 封装特定领域的知识和流程
- 组合多个工具完成复杂任务
- 独立测试和版本管理
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMClient(Protocol):
    """LLM 客户端协议。"""

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        """发送聊天请求。"""
        ...


@dataclass
class SkillContext:
    """技能执行上下文。"""

    llm: LLMClient
    memory: Any | None = None
    variables: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillResult:
    """技能执行结果。"""

    output: str
    success: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class Skill:
    """技能基类。

    子类需要实现 execute() 方法。
    """

    name: str = "base_skill"
    description: str = "基础技能"
    version: str = "1.0.0"

    def __init__(self) -> None:
        self._tools: dict[str, Callable] = {}

    def register_tool(self, name: str, func: Callable) -> None:
        """注册工具到技能。"""
        self._tools[name] = func

    async def execute(self, input_text: str, context: SkillContext) -> SkillResult:
        """执行技能。

        Args:
            input_text: 输入文本
            context: 执行上下文

        Returns:
            SkillResult 执行结果
        """
        raise NotImplementedError

    def get_schema(self) -> dict[str, Any]:
        """获取技能 Schema（用于 LLM 理解）。"""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
        }


class PromptSkill(Skill):
    """提示词技能：基于模板和 LLM 的技能。"""

    def __init__(
        self,
        name: str,
        description: str,
        template: str,
        version: str = "1.0.0",
    ) -> None:
        super().__init__()
        self.name = name
        self.description = description
        self.template = template
        self.version = version

    async def execute(self, input_text: str, context: SkillContext) -> SkillResult:
        """执行提示词技能。"""
        prompt = self.template.format(input=input_text, **context.variables)

        messages = [
            {"role": "system", "content": f"你是{self.name}专家。{self.description}"},
            {"role": "user", "content": prompt},
        ]

        response = await context.llm.chat(messages)
        output = response["choices"][0]["message"].get("content", "")

        return SkillResult(
            output=output,
            metadata={"skill": self.name, "version": self.version},
        )


class ToolSkill(Skill):
    """工具技能：组合多个工具完成复杂任务。"""

    def __init__(
        self,
        name: str,
        description: str,
        workflow: list[dict[str, Any]],
        version: str = "1.0.0",
    ) -> None:
        """初始化。

        Args:
            name: 技能名称
            description: 技能描述
            workflow: 工作流步骤 [{"tool": "tool_name", "args": {...}}, ...]
            version: 版本号
        """
        super().__init__()
        self.name = name
        self.description = description
        self.workflow = workflow
        self.version = version

    async def execute(self, input_text: str, context: SkillContext) -> SkillResult:
        """按工作流执行工具链。"""
        results = []
        variables = {"input": input_text, **context.variables}

        for step in self.workflow:
            tool_name = step["tool"]
            tool = self._tools.get(tool_name)

            if tool is None:
                return SkillResult(
                    output=f"工具 '{tool_name}' 未注册",
                    success=False,
                )

            # 渲染参数模板
            args = {}
            for key, value in step.get("args", {}).items():
                if isinstance(value, str) and "{" in value:
                    args[key] = value.format(**variables)
                else:
                    args[key] = value

            # 执行工具
            if inspect.iscoroutinefunction(tool):
                result = await tool(**args)
            else:
                result = tool(**args)

            results.append({"tool": tool_name, "result": result})
            variables[f"{tool_name}_result"] = result

        # 汇总结果
        output = "\n".join(f"[{r['tool']}] {r['result']}" for r in results)

        return SkillResult(
            output=output,
            metadata={"skill": self.name, "steps": len(self.workflow)},
        )


class SkillRegistry:
    """技能注册中心。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """注册技能。"""
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        """获取技能。"""
        return self._skills.get(name)

    def list_skills(self) -> list[str]:
        """列出所有技能名。"""
        return list(self._skills.keys())

    def list_schemas(self) -> list[dict[str, Any]]:
        """列出所有技能 Schema。"""
        return [skill.get_schema() for skill in self._skills.values()]

    async def execute(self, name: str, input_text: str, context: SkillContext) -> SkillResult:
        """执行指定技能。"""
        skill = self.get(name)
        if skill is None:
            return SkillResult(output=f"技能 '{name}' 不存在", success=False)
        return await skill.execute(input_text, context)


# 预定义技能库
BUILTIN_SKILLS = {
    "summarize": PromptSkill(
        name="summarize",
        description="文本摘要",
        template="请对以下内容进行摘要，提取关键信息：\n\n{input}",
    ),
    "translate": PromptSkill(
        name="translate",
        description="中英互译",
        template="将以下内容翻译成中文（如果是英文）或英文（如果是中文）：\n\n{input}",
    ),
    "code_review": PromptSkill(
        name="code_review",
        description="代码审查",
        template="请审查以下代码，检查 bug、性能、风格问题：\n\n```\n{input}\n```",
    ),
    "extract_keywords": PromptSkill(
        name="extract_keywords",
        description="关键词提取",
        template="从以下内容中提取 5 个关键词，用逗号分隔：\n\n{input}",
    ),
}
