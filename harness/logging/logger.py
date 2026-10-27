"""日志配置：structlog + 控制台输出。"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: str = "INFO") -> None:
    """初始化结构化日志。

    输出固定走 stderr：stdout 保留给业务输出（CLI 流式文本、管道消费），
    工具调用等运行日志不污染两者。
    """
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """获取 logger 实例。"""
    return structlog.get_logger(name)
