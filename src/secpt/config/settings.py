"""pydantic-settings 根配置。

加载优先级：
    env (SECPT_xxx__yyy) > config.yaml > 模型默认值

YAML source 通过 :class:`YamlConfigSource` 注入。

使用方式::

    from secpt.config.settings import get_settings
    settings = get_settings()
    print(settings.llm.planning_model)

环境变量示例::

    SECPT_LLM__EVAL_MODEL=deepseek-reasoner
    SECPT_LLM__FAKE=true
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from secpt.config.models import (
    AppConfig,
    AuditConfig,
    LLMConfig,
    LoggingConfig,
    PricingConfig,
)

_ROOT_YAML = Path(__file__).resolve().parents[3] / "config.yaml"


class YamlConfigSource(PydanticBaseSettingsSource):
    """从 YAML 文件读取顶层字段（key = Settings 字段名）。"""

    def __call__(self) -> dict[str, Any]:
        if not _ROOT_YAML.is_file():
            return {}
        with _ROOT_YAML.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in self.settings_cls.model_fields}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # 非字段级 source，走整字典路径
        return None, field_name, False  # pragma: no cover


class Settings(BaseSettings):
    """secpt 全局配置。"""

    model_config = SettingsConfigDict(
        env_prefix="SECPT_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[Settings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env > yaml > init > defaults
        return (
            env_settings,
            YamlConfigSource(settings_cls),
            init_settings,
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例获取配置（Phase 0 占位，后续可加 reload）。"""
    return Settings()
