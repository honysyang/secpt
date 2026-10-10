"""LLM 工厂。

通过 :class:`LLMConfig` 决定实例：
- ``fake=True`` → :class:`FakeLLM`（空 script，调用方后续注入）
- 否则       → :class:`LiteLLMAdapter`
"""

from __future__ import annotations

from secpt.config.models import LLMConfig
from secpt.core.llm.base import BaseLLM
from secpt.core.llm.fake import FakeLLM
from secpt.core.llm.litellm_adapter import LiteLLMAdapter


def get_llm(config: LLMConfig) -> BaseLLM:
    """根据配置返回适配器实例。"""
    if config.fake:
        return FakeLLM(script=[])
    return LiteLLMAdapter(config)
