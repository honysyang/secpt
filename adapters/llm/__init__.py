"""LLM 适配器模块：统一接入各家大模型。"""

from adapters.llm.factory import build_llm

__all__ = ["build_llm"]
