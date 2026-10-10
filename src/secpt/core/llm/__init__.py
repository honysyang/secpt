"""LLM 调用边界。

Phase 1 仅实现调用边界：
- :class:`LLMMessage` —— 统一消息结构
- :class:`BaseLLM` —— 抽象异步接口
- :class:`LiteLLMAdapter` —— litellm → DeepSeek 适配
- :class:`FakeLLM` —— 本地脚本化替身（测试 / dry-run）
- :func:`get_llm` —— 工厂
"""

from __future__ import annotations

from secpt.core.llm.base import BaseLLM, LLMError, LLMMessage
from secpt.core.llm.factory import get_llm
from secpt.core.llm.fake import FakeLLM
from secpt.core.llm.litellm_adapter import LiteLLMAdapter

__all__ = [
    "BaseLLM",
    "FakeLLM",
    "LLMError",
    "LLMMessage",
    "LiteLLMAdapter",
    "get_llm",
]
