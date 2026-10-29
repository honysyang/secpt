"""LLM 接口层：统一 chat 协议的所有出口（开发文档 §6.1）。

分层红线：llms 只可被 core 导入，可导入 store/errors；反向 import 即架构破坏。
"""

from secpt.llms.client import (
    TEMPERATURE_FREESTYLE,
    TEMPERATURE_PLAN,
    Goal,
    LlmClient,
    LlmProvider,
    Message,
    StepOutput,
    ToolCall,
)
from secpt.llms.fake import FakeLLM
from secpt.llms.providers import OpenAICompatProvider

__all__ = [
    "TEMPERATURE_FREESTYLE",
    "TEMPERATURE_PLAN",
    "FakeLLM",
    "Goal",
    "LlmClient",
    "LlmProvider",
    "Message",
    "OpenAICompatProvider",
    "StepOutput",
    "ToolCall",
]
