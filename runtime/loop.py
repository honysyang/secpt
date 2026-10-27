"""执行循环：驱动 Agent 运行的核心引擎。

支持两种模式：
1. ReAct 模式：边想边做，每步决策依赖上一步结果
2. Plan-and-Execute 模式：先规划再执行

循环终止条件：
- 达到最大步数
- 输出最终答案
- 触发停止事件
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from harness.planner.base import Plan
from harness.tools.registry import ToolRegistry


@dataclass
class LoopResult:
    """循环执行结果。"""

    output: str
    steps: list[dict[str, Any]]
    total_tokens: int = 0
    # 本轮完整消息历史（含 system/user/assistant/tool）。
    # 调用方保存它并在下一轮经 history= 传回，即可实现跨轮会话记忆
    messages: list[dict[str, Any]] | None = None


class LLMClient(Protocol):
    """LLM 客户端协议。"""

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        """发送聊天请求，返回响应。"""
        ...


class ExecutionLoop:
    """执行循环引擎。"""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        max_steps: int = 10,
        hitl_callback: Any | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        context_budget_chars: int = 60_000,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        self.hitl_callback = hitl_callback  # 人工确认回调
        self.on_token = on_token  # 流式回调：LLM 文本增量时触发
        self.on_event = on_event  # 过程事件回调：tool_call / tool_result / tool_rejected
        # 上下文预算（字符数，≈1/4 token）：超限时裁剪旧轮次观察，
        # 防止长任务消息无限增长逼近模型窗口
        self.context_budget_chars = context_budget_chars

    def _emit(self, event: dict[str, Any]) -> None:
        """向 UI/观测层推送过程事件；无订阅者时零开销。"""
        if self.on_event is not None:
            self.on_event(event)

    async def run_react(
        self,
        task: str,
        context: str = "",
        history: list[dict[str, Any]] | None = None,
    ) -> LoopResult:
        """ReAct 模式：Thought → Action → Observation 循环。

        Args:
            task: 任务描述
            context: 额外上下文
            history: 可选的跨轮会话历史（上一轮 result.messages）。
                     传入后本轮在历史之上追加新任务，实现会话记忆

        Returns:
            LoopResult 包含最终输出、执行步骤和完整消息历史
        """
        if history:
            # 跨轮会话：在历史之上追加新任务（跳过上一轮的 system，防重复）
            messages = [m for m in history if m.get("role") != "system"]
            messages.insert(0, {"role": "system", "content": "你是一个智能助手。使用工具完成任务，然后给出最终答案。"})
            messages.append({"role": "user", "content": task})
        else:
            messages = self._build_messages(task, context)
        steps: list[dict[str, Any]] = []
        total_tokens = 0

        for step_num in range(1, self.max_steps + 1):
            # 上下文预算：超限时裁剪旧轮次（保留 system 与最近消息）
            self._trim_messages(messages)
            # Thought: LLM 思考下一步（支持流式）
            # chat_stream 可能被韧性包装器显式置为 None（底层不支持流式），
            # 因此用 getattr 判断而非 hasattr
            if self.on_token is not None and getattr(self.llm, "chat_stream", None) is not None:
                response = await self._chat_stream(messages)
            else:
                response = await self.llm.chat(messages, tools=self.tools.list_schemas() or None)
            total_tokens += response.get("usage", {}).get("total_tokens", 0)

            thought = response["choices"][0]["message"].get("content", "")
            tool_calls = response["choices"][0]["message"].get("tool_calls", [])

            steps.append({
                "step": step_num,
                "type": "thought",
                "content": thought,
            })

            # 如果没有工具调用，说明已完成
            if not tool_calls:
                return LoopResult(
                    output=thought, steps=steps, total_tokens=total_tokens, messages=messages
                )

            # 关键：assistant 的 tool_calls 消息必须进入历史，
            # 否则下一轮请求中 tool 消息将失去前置 tool_calls
            # （DeepSeek 会报 400: tool must follow tool_calls）
            messages.append(response["choices"][0]["message"])

            # Action: 执行工具
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                tool_args_str = tool_call["function"].get("arguments", "{}")

                steps.append({
                    "step": step_num,
                    "type": "action",
                    "tool": tool_name,
                    "args": tool_args_str,
                })

                # 执行工具
                self._emit({"type": "tool_call", "tool": tool_name, "args": tool_args_str})
                tool = self.tools.get(tool_name)
                if tool is None:
                    observation = f"错误: 工具 '{tool_name}' 不存在"
                else:
                    # HITL: 危险工具需要人工确认
                    # 回调来源优先级：显式 hitl_callback > 注册表上的 approval_hook
                    hook = self.hitl_callback or getattr(self.tools, "approval_hook", None)
                    if tool.dangerous and hook:
                        approved = await hook(tool_name, tool_args_str)
                        if not approved:
                            observation = f"用户拒绝了执行 '{tool_name}'"
                            self._emit({"type": "tool_rejected", "tool": tool_name})
                            steps.append({
                                "step": step_num,
                                "type": "observation",
                                "content": observation,
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.get("id", ""),
                                "content": observation,
                            })
                            continue

                    try:
                        args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                        observation = await tool.execute(**args)
                    except Exception as e:
                        observation = f"工具执行错误: {e}"

                self._emit({"type": "tool_result", "tool": tool_name, "content": str(observation)})
                steps.append({
                    "step": step_num,
                    "type": "observation",
                    "content": str(observation),
                })

                # 添加工具结果到消息
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", ""),
                    "content": str(observation),
                })

        # 达到最大步数：再做一次无工具的收敛总结。
        # 此时 messages 里已有完整的执行历史，LLM 能基于全部观察给出结论，
        # 而不是干巴巴地返回"达到最大步数限制"丢掉已收集的信息。
        messages.append({
            "role": "user",
            "content": (
                f"任务: {task}\n\n"
                "你已用完工具调用次数。请基于以上全部执行结果，"
                "直接给出最终回答（不再调用工具）。"
            ),
        })
        summary = await self.llm.chat(messages)
        total_tokens += summary.get("usage", {}).get("total_tokens", 0)
        final_output = (
            summary["choices"][0]["message"].get("content", "").strip()
            or "达到最大步数限制，且模型未给出总结"
        )
        return LoopResult(
            output=final_output,
            steps=steps,
            total_tokens=total_tokens,
            messages=messages,
        )

    async def _chat_stream(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """流式调用 LLM：文本增量转发给 on_token，聚合出完整响应。

        与 chat() 返回相同的统一格式，循环逻辑无需感知流式与否。
        """
        final: dict[str, Any] | None = None
        async for event in self.llm.chat_stream(messages, tools=self.tools.list_schemas() or None):
            if event["type"] == "delta":
                self.on_token(event["content"])  # type: ignore[union-attr]
            elif event["type"] == "final":
                final = event
        if final is None:  # 防御：适配器未按协议产出 final
            raise RuntimeError("LLM 流式响应缺少 final 事件")
        return {
            "choices": [{"message": final["message"]}],
            "usage": final.get("usage", {}),
        }

    async def run_plan_execute(self, plan: Plan, context: str = "") -> LoopResult:
        """Plan-and-Execute 模式：按预定计划执行。

        Args:
            plan: 执行计划
            context: 额外上下文

        Returns:
            LoopResult 包含执行结果
        """
        steps: list[dict[str, Any]] = []
        total_tokens = 0
        results: dict[str, Any] = {}

        for step_num, step in enumerate(plan.steps, 1):
            # 构建步骤消息
            messages = [
                {"role": "system", "content": f"执行步骤: {step.action}"},
                {"role": "user", "content": f"目标: {plan.goal}\n上下文: {context}\n上一步结果: {results}"},
            ]

            response = await self.llm.chat(messages)
            total_tokens += response.get("usage", {}).get("total_tokens", 0)
            output = response["choices"][0]["message"].get("content", "")

            results[step.action] = output
            steps.append({
                "step": step_num,
                "type": "execute",
                "action": step.action,
                "output": output,
            })

        # 汇总结果
        summary_messages = [
            {"role": "system", "content": "汇总执行结果"},
            {"role": "user", "content": f"目标: {plan.goal}\n各步骤结果: {results}"},
        ]
        summary_response = await self.llm.chat(summary_messages)
        final_output = summary_response["choices"][0]["message"].get("content", "")

        return LoopResult(output=final_output, steps=steps, total_tokens=total_tokens, messages=messages)

    def _build_messages(self, task: str, context: str) -> list[dict[str, str]]:
        """构建初始消息列表。"""
        messages = [
            {"role": "system", "content": "你是一个智能助手。使用工具完成任务，然后给出最终答案。"},
        ]
        if context:
            messages.append({"role": "user", "content": f"上下文: {context}"})
        messages.append({"role": "user", "content": task})
        return messages

    def _trim_messages(self, messages: list[dict[str, Any]]) -> None:
        """上下文超预算时就地裁剪旧轮次。

        策略：system 消息与最近 keep_recent 条消息原样保留，
        其余更早的 tool/assistant 消息内容截断为前 500 字符加标记。
        结构（role/tool_call_id 配对）不动，避免破坏 DeepSeek 的
        tool_calls → tool 相邻性校验。
        """
        total = sum(len(str(m.get("content", ""))) for m in messages)
        if total <= self.context_budget_chars:
            return

        keep_recent = 6  # 最近 3 轮（assistant + tool）保持完整
        cut_from = 1  # 跳过 system
        cut_to = max(cut_from, len(messages) - keep_recent)
        marker = "\n... [已因上下文预算截断]"

        for m in messages[cut_from:cut_to]:
            content = str(m.get("content", ""))
            if len(content) > 500:
                m["content"] = content[:500] + marker
