"""structlog 配置。

约定：
- 控制台：人类可读
- ``logging.json=True`` 时切换 JSON 输出（便于集中采集）
- 单元测试默认走 ``caplog``，无需特殊处理
"""

from __future__ import annotations

import logging
import sys

import structlog

from secpt.config.models import LoggingConfig


def configure_logging(cfg: LoggingConfig | None = None) -> None:
    """按配置初始化 structlog + 标准 logging。"""
    level_name = (cfg.level if cfg else "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    json_mode = bool(cfg.json_output) if cfg else False

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=False)

    shared_processors: list[object] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if json_mode:
        renderer: object = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)

    # 静默 httpx / litellm 的 noisy 日志
    for noisy in ("httpx", "httpcore", "litellm", "openai"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
