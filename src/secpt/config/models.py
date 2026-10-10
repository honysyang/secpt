"""各子配置模型。

Phase 0/1 字段按最小可用集定义，后续按需扩展。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    name: str = "secpt"
    version: str = "0.1.0"
    workdir: str = "./data/runs"


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


class GraphConfig(BaseModel):
    """图存储配置。

    只放连接参数，绝不放密钥；密钥若有，凭证明走 env。
    ``backend`` 切换存储后端：memory / sqlite / postgres（后两者留待 4b/4c）。
    """

    backend: Literal["memory", "sqlite", "postgres"] = "memory"
    sqlite_path: str = "./data/graphstore.sqlite"
    dsn: str = ""

    class SQLite(BaseModel):
        path: str = "./data/graphstore.sqlite"
        echo: bool = False

    class Postgres(BaseModel):
        dsn: str = ""
        pool_size: int = 5

    sqlite: SQLite = Field(default_factory=SQLite)
    postgres: Postgres = Field(default_factory=Postgres)


class ReactConfig(BaseModel):
    max_steps: int = 12
    timeout_s: int = 600
    early_stop_on_finish: bool = True
    reflection: bool = False


class ToolsConfig(BaseModel):
    """工具装配配置。

    - ``command_whitelist``: 走 subprocess 的命令基名（nmap/curl/dig 等）
    - ``tool_names``: 注册进 Registry 的工具名（含纯 Python 工具）
    - ``whitelist``: 向后兼容别名，等价于 command_whitelist
    """

    model_config = {"populate_by_name": True}

    command_whitelist: list[str] = Field(default_factory=list, alias="whitelist")
    tool_names: list[str] = Field(default_factory=lambda: [
        "read_file", "list_dir", "nmap", "curl", "dig", "whatweb", "whois",
        "crawler", "http_analyze", "saml", "flag_extract",
    ])
    default_timeout: float = 30.0
    exec_timeout_s: int = 120
    # 逐工具超时覆盖（秒）
    tool_timeouts: dict[str, float] = Field(default_factory=lambda: {
        "nmap": 45.0,
        "whatweb": 20.0,
        "crawler": 30.0,
        "curl": 15.0,
        "whois": 10.0,
        "dig": 10.0,
    })
    sandbox_enabled: bool = False
    sandbox_backend: str | None = None

    @property
    def whitelist(self) -> list[str]:
        """向后兼容：whitelist 是 command_whitelist 的别名。"""
        return self.command_whitelist


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

    input_per_1k: float = 0.002      # deepseek-chat 输入（缓存未命中）单价
    output_per_1k: float = 0.008     # deepseek-chat 输出单价


class AuditConfig(BaseModel):
    enabled: bool = True
    path: str = "./data/audit"
