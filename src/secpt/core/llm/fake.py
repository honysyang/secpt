"""脚本化假 LLM（测试 / dry-run）。

行为：
- 构造时传 ``script``（按调用次序弹出）或 ``generator``（按历史生成）
- script 用完后循环最后一条，并 structlog 告警（同时 ``warnings.warn`` 便于测试断言）
- ``acompletion`` 不发起任何网络请求
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

import structlog

from secpt.core.llm.base import BaseLLM, LLMMessage

logger = structlog.get_logger(__name__)


class FakeLLM(BaseLLM):
    """本地替身：按预设脚本或回调返回 assistant 消息。"""

    def __init__(
        self,
        *,
        script: list[LLMMessage] | None = None,
        generator: Callable[[list[LLMMessage]], LLMMessage] | None = None,
    ) -> None:
        if script is None and generator is None:
            raise ValueError("FakeLLM 需要 script 或 generator 之一")
        self._script: list[LLMMessage] = list(script) if script is not None else []
        self._generator = generator
        self._call_index = 0

    async def acompletion(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMMessage:
        self._call_index += 1
        if self._generator is not None:
            return self._generator(messages)

        if not self._script:
            raise RuntimeError("FakeLLM script 为空且未提供 generator")

        if self._call_index <= len(self._script):
            return self._script[self._call_index - 1]

        # 超过 script 长度：循环最后一条 + 告警
        last = self._script[-1]
        msg = (
            f"FakeLLM script exhausted (call_index={self._call_index}, "
            f"script_len={len(self._script)}); reusing last message"
        )
        logger.warning(
            "fake_llm.script_exhausted",
            call_index=self._call_index,
            script_len=len(self._script),
            reused_content=last.content[:80],
        )
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        return last
