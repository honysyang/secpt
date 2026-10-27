"""配置管理：环境变量 + 默认值。

模块导入时自动加载项目根的 .env（python-dotenv），
任何入口（CLI / 测试 / 库调用）无需各自处理凭证加载。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 以本文件位置锚定项目根，避免受启动目录影响
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass
class Settings:
    """应用配置。"""

    # LLM
    llm_provider: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-20241022"

    # 存储
    memory_backend: str = "memory"  # memory | redis | postgres
    vector_backend: str = "local"  # local | pinecone | qdrant

    # 日志
    log_level: str = "INFO"

    # 环境
    env: str = "dev"


@lru_cache
def get_settings() -> Settings:
    """获取配置单例。"""
    return Settings(
        llm_provider=os.getenv("LLM_PROVIDER", "deepseek"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
        memory_backend=os.getenv("MEMORY_BACKEND", "memory"),
        vector_backend=os.getenv("VECTOR_BACKEND", "local"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        env=os.getenv("APP_ENV", "dev"),
    )
