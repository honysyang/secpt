"""子智能体：可独立执行任务的 Agent 封装。

架构分层：
    SubAgent (业务层) → ExecutionLoop (引擎层) → LLMClient (适配层)

SubAgent 不直接调用 LLM，而是委托给 ExecutionLoop 执行 ReAct 循环，
自身只负责：角色定义、工具集、结果封装。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from harness.tools.registry import ToolRegistry


class LLMClient(Protocol):
    """LLM 客户端协议。"""

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        """发送聊天请求。"""
        ...


@dataclass
class AgentResult:
    """子 Agent 执行结果。"""

    output: str
    agent_name: str
    tokens_used: int = 0
    metadata: dict[str, Any] | None = None


class SubAgent:
    """子智能体：执行特定领域任务。

    每个 SubAgent 有独立的：
    - 系统提示词（角色定义）
    - 工具集
    - LLM 客户端（通过 ExecutionLoop 调用）

    执行流程：
        run() → ExecutionLoop.run_react() → LLM + Tools → AgentResult
    """

    def __init__(
        self,
        name: str,
        instructions: str,
        llm: LLMClient,
        tools: ToolRegistry | None = None,
        max_steps: int = 10,
        description: str = "",
    ) -> None:
        self.name = name
        self.instructions = instructions
        self.llm = llm
        self.tools = tools or ToolRegistry()
        self.max_steps = max_steps
        self.description = description  # 一句话职责，供路由决策参考

    async def run(
        self,
        task: str,
        context: str = "",
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentResult:
        """执行任务。

        委托给 ExecutionLoop 执行 ReAct 循环，包含：
        - Thought: LLM 思考下一步
        - Action: 调用工具
        - Observation: 观察结果

        Args:
            task: 任务描述
            context: 额外上下文
            on_token: 可选的流式回调，收到 LLM 文本增量时调用
            on_event: 可选的过程事件回调（tool_call/tool_result/tool_rejected），
                供 UI 展示交互过程；审批仍走工具注册表的 approval_hook

        Returns:
            AgentResult 包含输出和元数据
        """
        from runtime.loop import ExecutionLoop

        loop = ExecutionLoop(
            llm=self.llm,
            tools=self.tools,
            max_steps=self.max_steps,
            on_token=on_token,
            on_event=on_event,
        )
        result = await loop.run_react(task, context)

        return AgentResult(
            output=result.output,
            agent_name=self.name,
            tokens_used=result.total_tokens,
            metadata={"steps": result.steps},
        )

    async def run_simple(self, task: str, context: str = "") -> AgentResult:
        """简单执行：不使用工具，单次 LLM 调用。

        适合不需要工具调用的场景（如文本生成、问答）。

        Args:
            task: 任务描述
            context: 额外上下文

        Returns:
            AgentResult 包含输出和元数据
        """
        messages = [
            {"role": "system", "content": self.instructions},
        ]
        if context:
            messages.append({"role": "user", "content": f"上下文: {context}"})
        messages.append({"role": "user", "content": task})

        response = await self.llm.chat(messages)
        output = response["choices"][0]["message"].get("content", "")
        tokens = response.get("usage", {}).get("total_tokens", 0)

        return AgentResult(
            output=output,
            agent_name=self.name,
            tokens_used=tokens,
        )
