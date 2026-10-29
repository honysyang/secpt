"""统一 chat 协议（开发文档 §6.1：所有器官同一协议，provider 一换全换）。

分层动机（为什么主循环只见 :class:`LlmClient`）：
- provider 只实现一个最小原语 ``complete(messages) -> str``，把"结构化输出解析 +
  schema 带错重试 + 超时指数退避"这类与 provider 无关的纪律收敛在协议层一处，
  避免每个器官各抄一份重试逻辑。
- ``Any`` 只允许出现在 LLM 响应边界（provider 原始文本 → pydantic 解析处），对上层
  一律强类型——这是开发文档钉死的豁免边界，不越。
- provider 抛 ``TimeoutError`` 由协议层转指数退避（≤3 次），仍失败上抛
  ``LlmError(E_TIMEOUT_LLM)``；schema 校验失败带错重试（≤2 次），耗尽上抛
  ``LlmError(E_SCHEMA)``。两种上限都对应错误矩阵 §9 的落账语义。
- 消息列表 append-only：重试时复制 append，绝不原地改调用方的 messages。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, Literal, Protocol, TypeVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from secpt.errors import LlmError

_log = structlog.get_logger()

# 温度分档（§3.7）：规划档 0~0.2，发散档 0.8。协议层只透传，档位由调用方（loop）选择。
TEMPERATURE_PLAN = 0.2
TEMPERATURE_FREESTYLE = 0.8

Role = Literal["system", "user", "assistant"]

T = TypeVar("T", bound=BaseModel)


class Message(BaseModel):
    """单条聊天消息。role/content 之外不设字段，保证 provider 层可无脑序列化。"""

    role: Role
    content: str


class ToolCall(BaseModel):
    """主 LLM 一步输出里的单个工具点名。M1 只需要 name+args，argv 转换归适配器。"""

    name: str
    args: dict[str, str | int | float] = Field(default_factory=dict)


class Goal(BaseModel):
    """进场三问固化结果（WF1）：缺项追问由上层负责，本模型只管三字段齐全。"""

    target: str
    objective: str
    success_criteria: str


class StepOutput(BaseModel):
    """主 LLM·一步的结构化输出（WF2）：thought + 规划增补/收口 + tool_calls(≤4) + done。

    ``plan_done`` / ``plan_cancel``（M5）：对已有规划条目的收口声明——值是规划条目的
    短 id（注意力包里 ``[id=xxxxxxxx]``）或原文，store 侧两种都认。上限 8 条/步：
    规划收口是低频动作，给个上界防模型刷屏。它们都是可选字段，老脚本不受影响。
    """

    thought: str
    plan_updates: list[str] = Field(default_factory=list)
    plan_done: list[str] = Field(default_factory=list, max_length=8)
    plan_cancel: list[str] = Field(default_factory=list, max_length=8)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=4)
    done: bool = False


class LlmProvider(Protocol):
    """Provider 最小原语：给消息列表，回一段文本。超时抛 ``TimeoutError``。"""

    def complete(self, messages: Sequence[Message], *, temperature: float, timeout_s: float) -> str:
        """同步补全一次。provider 不解析 schema，也不重试——那些是协议层的职责。"""
        ...


_SCHEMA_REPAIR_NUDGE = (
    "你的上一次输出没有通过 JSON Schema 校验，错误是：\n{error}\n"
    "请重新只输出一个符合上述 schema 的 JSON 对象，不要输出任何多余文本。"
)


class LlmClient:
    """协议层：所有 LLM 器官的统一入口。

    职责划分：provider 负责"连得上"，本类负责"守纪律"（schema 重试/退避/温度透传）。
    器官 facade（main_llm.step 等）在本类之上薄封装，M1 先落 step 用的
    :meth:`structured` 与 :class:`~secpt.llms.schemas` 两个输出模型。
    """

    def __init__(
        self,
        provider: LlmProvider,
        *,
        max_schema_retries: int = 2,
        max_timeout_retries: int = 3,
        backoff_base_s: float = 1.0,
    ) -> None:
        """``backoff_base_s`` 仅测试注入调小；生产一律走默认 1 秒指数档。"""
        self._provider = provider
        self._max_schema_retries = max_schema_retries
        self._max_timeout_retries = max_timeout_retries
        self._backoff_base_s = backoff_base_s

    def structured(
        self,
        messages: Sequence[Message],
        schema: type[T],
        *,
        temperature: float,
        timeout_s: float = 60.0,
    ) -> T:
        """带 schema 重试的结构化调用：合法即返回，带错重试≤2 后上抛 E_SCHEMA。

        重试消息 = 原消息 + 助手原话 + 修错指令，全部走复制 append（append-only）。
        """
        convo: list[Message] = list(messages)
        last_error = ""
        total_attempts = 1 + self._max_schema_retries
        for attempt in range(1, total_attempts + 1):
            raw = self._complete_with_backoff(convo, temperature=temperature, timeout_s=timeout_s)
            try:
                return schema.model_validate(_parse_json_response(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                # 带错重试：把解析/校验错误原文塞回去让模型自己修，这是防抖五件之一。
                last_error = _display_error(exc)
                _log.info("schema_retry", attempt=attempt, error=last_error)
                convo.append(Message(role="assistant", content=raw))
                convo.append(
                    Message(role="user", content=_SCHEMA_REPAIR_NUDGE.format(error=last_error))
                )
        raise LlmError(
            "LLM 输出反复不合 schema",
            code="E_SCHEMA",
            context={"attempts": total_attempts, "last_error": last_error},
        )

    def _complete_with_backoff(
        self, messages: Sequence[Message], *, temperature: float, timeout_s: float
    ) -> str:
        """超时指数退避≤3 次；仍失败上抛 E_TIMEOUT_LLM（错误矩阵 §9 → audit_log）。"""
        last_exc: TimeoutError | None = None
        for attempt in range(1, self._max_timeout_retries + 1):
            try:
                return self._provider.complete(
                    messages, temperature=temperature, timeout_s=timeout_s
                )
            except TimeoutError as exc:
                last_exc = exc
                delay = self._backoff_base_s * (2 ** (attempt - 1))
                _log.warning("llm_timeout_backoff", attempt=attempt, delay_s=delay)
                if attempt < self._max_timeout_retries:
                    time.sleep(delay)
        raise LlmError(
            "LLM 超时退避耗尽",
            code="E_TIMEOUT_LLM",
            context={"attempts": self._max_timeout_retries, "last": str(last_exc)},
        )


def _display_error(exc: Exception) -> str:
    """给模型看的错误摘要：ValidationError 展开成逐字段人话，解析错原样。"""
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return str(exc)


def _parse_json_response(raw: str) -> Any:  # noqa: ANN401 — LLM 响应边界豁免
    """provider 文本 → dict：剥掉常见 ```json 围栏后再解析。``Any`` 边界豁免点。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


__all__ = [
    "TEMPERATURE_FREESTYLE",
    "TEMPERATURE_PLAN",
    "Goal",
    "LlmClient",
    "LlmProvider",
    "Message",
    "StepOutput",
    "ToolCall",
]
