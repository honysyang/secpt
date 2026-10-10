"""LLM 抽象层：消息结构与适配器契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from secpt.telemetry.token_tracker import TokenTracker


class LLMError(Exception):
    """LLM 调用失败（缺 key、限流、API 错、超时、配置错）。"""


Role = Literal["system", "user", "assistant", "tool"]


class LLMMessage:
    """一条 chat 消息（极简，Phase 1 不需要 pydantic 校验）。"""

    __slots__ = ("content", "role", "tool_call_id", "tool_calls")

    def __init__(
        self,
        role: Role,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        self.role: Role = role
        self.content: str = content
        self.tool_calls: list[dict[str, Any]] | None = tool_calls
        self.tool_call_id: str | None = tool_call_id

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"LLMMessage(role={self.role!r}, content={self.content!r}, tool_calls={self.tool_calls!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LLMMessage):
            return NotImplemented
        return (
            self.role == other.role
            and self.content == other.content
            and self.tool_calls == other.tool_calls
            and self.tool_call_id == other.tool_call_id
        )


class BaseLLM(ABC):
    """所有 LLM 适配器的最小接口。"""

    def __init__(self) -> None:
        self._tracker: TokenTracker | None = None

    def set_tracker(self, tracker: TokenTracker) -> None:
        """绑定 token 追踪器。"""
        self._tracker = tracker

    def _report_tokens(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """上报 token 消耗。"""
        if self._tracker is not None:
            self._tracker.record(model, prompt_tokens, completion_tokens)

    @abstractmethod
    async def acompletion(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMMessage:
        """调用一次 chat completion，返回规整后的 assistant 消息。

        :param messages: 完整历史，按 OpenAI chat 顺序
        :param model:    覆盖默认模型；None 用适配器自带默认
        :param temperature: 覆盖温度
        :param max_tokens:  覆盖最大 token
        :param tools:   OpenAI function schema 列表；非空时启用结构化调用
        :raises LLMError: 任何调用失败统一抛 :class:`LLMError`
        """
        raise NotImplementedError
