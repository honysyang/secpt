"""结构化日志装配：engine/worker 生命周期日志统一走 structlog。

- 默认 ConsoleRenderer（stderr，人读，视觉上接近原 print）
- SECPT_LOG_JSON=1 → JSONRenderer（采集器按 job_id 检索链路）
- configure 幂等可重复调用（cache_logger_on_first_use=False，env 切换即生效）
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging() -> None:
    """装配 structlog（幂等）。JSON 与否由 SECPT_LOG_JSON 控制，输出到 stderr。"""
    json_mode = os.environ.get("SECPT_LOG_JSON") == "1"
    processors = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="ts"),
    ]
    renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_mode
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
