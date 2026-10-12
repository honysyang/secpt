"""各子配置模型。

Phase 0/1 字段按最小可用集定义，后续按需扩展。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    name: str = "secpt"
    version: str = "0.1.0"
    workdir: str = "./var/runs"


class LLMConfig(BaseModel):
    """LLM 调用配置。

    ``api_key`` 不写在这里，从环境变量 ``api_key_env``（默认 DEEPSEEK_API_KEY）读取。
    ``eval_model`` 不限制具体模型，默认 ``deepseek-v1-flash``（便宜/快，用于打分、总结类场景）；
    如需更好效果可通过 ``SECPT_LLM__EVAL_MODEL`` 覆盖。
    """

    provider: str = "deepseek"
    planning_model: str = "deepseek-chat"
    eval_model: str = "deepseek-v1-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout_s: float = 60.0
    fake: bool = False


class LoggingConfig(BaseModel):
    """日志配置。

    字段 ``json`` 与 ``BaseModel.json()`` 冲突，所以属性名用 ``json_output``，
    通过 alias 兼容 yaml/env 里的 ``json`` 键。
    """

    model_config = {"populate_by_name": True}

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_output: bool = Field(default=False, alias="json")
    file: str | None = None


class PricingConfig(BaseModel):
    """模型单价（CNY / 1K token）。S3 BudgetTracker 从这里读，不写死。"""

    input_per_1k: float = 0.002  # deepseek-chat 输入（缓存未命中）单价
    output_per_1k: float = 0.008  # deepseek-chat 输出单价


class AuditConfig(BaseModel):
    enabled: bool = True
    path: str = "./var/audit"
