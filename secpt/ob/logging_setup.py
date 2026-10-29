"""结构化日志装配（§8 可观测的机器频道）。

为什么从 cli_view 拆出来（M5-34 结构归位）：cli_view 是"事件订阅者 + 检查窗"，
日志装配是"进程级基础设施"——两者生命周期与变更节奏完全不同；混在一起让
"改日志格式"必须读 200 行订阅者代码。本模块只做三件事，谁都不依赖：

1. **行缓冲**：JSON（stderr）与人类气泡（stdout）是两条流，``2>&1 | tee`` 合流
   时块缓冲会把两行粘成一团（实测三轮 run.log 多处 JSON 粘在气泡行尾，机器
   解析直接报废）——两流都强制行缓冲，一行一行进管道；
2. **级别过滤**：``SECPT_LOG_LEVEL``（DEBUG/INFO/WARNING/ERROR，默认 INFO）；
3. **文件双写**：``SECPT_LOG_FILE`` 非空时，每条 JSON 事件同时追加到该文件
   （行缓冲）——审计不再依赖用户记得 tee；文件写失败只降级回 stderr，绝不
   让日志配置炸掉主流程。

与人类频道的关系：气泡（CliView → stdout）给人读过程，JSON 给机器做审计与
回放——两频道同源（同一 EventBus / 同一 store），互不污染（§8 纪律）。
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

import structlog

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class _StderrFactory:
    """每次实例化 logger 都现取 :data:`sys.stderr`（可含文件双写）。

    为什么不用 ``PrintLoggerFactory(file=sys.stderr)``：后者在 configure 时捕获
    stderr 的对象引用，而 pytest 等环境会整块替换 ``sys.stderr``，旧引用一旦
    闭合再写入就抛 ``ValueError: I/O operation on closed file``。动态解析把
    "哪个流是 stderr" 的裁决权推迟到每次写日志的那一刻。
    """

    def __init__(self, audit_file: TextIO | None = None) -> None:
        self._audit_file = audit_file

    def __call__(self, *args: object) -> structlog.PrintLogger:
        target: TextIO = sys.stderr
        if self._audit_file is not None:
            target = _MultiplexWriter(sys.stderr, self._audit_file)
        return structlog.PrintLogger(file=target)


class _MultiplexWriter:
    """stderr + 审计文件双写（M5-34）：JSON 事件一行两落，互不阻塞。

    文件侧任何异常（磁盘满/句柄被关）都降级为只写 stderr——审计文件是锦上
    添花，绝不能让日志子系统把主流程拖死。
    """

    def __init__(self, primary: TextIO, audit: TextIO) -> None:
        self._primary = primary
        self._audit = audit

    def write(self, text: str) -> int:
        written = self._primary.write(text)
        try:
            self._audit.write(text)
            self._audit.flush()
        except Exception:  # noqa: BLE001  审计文件故障降级为只写 stderr
            pass
        return written

    def flush(self) -> None:
        try:
            self._primary.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._audit.flush()
        except Exception:  # noqa: BLE001
            pass


def _log_level() -> int:
    """``SECPT_LOG_LEVEL`` → 数值；非法值静默回落 INFO（日志配置不配拒跑）。"""
    raw = os.environ.get("SECPT_LOG_LEVEL", "INFO").strip().upper()
    return _LEVELS.get(raw, 20)


def _audit_file() -> TextIO | None:
    """``SECPT_LOG_FILE`` → 追加写句柄（行缓冲）；未设置/打不开返回 None。"""
    path = os.environ.get("SECPT_LOG_FILE", "").strip()
    if not path:
        return None
    try:
        return open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 行缓冲追加
    except OSError:
        return None


def configure_logging() -> None:
    """structlog 全量 JSON 落 stderr（§8），可级别过滤与审计文件双写。

    行缓冲是给 ``2>&1 | tee`` 的：两流一行一行进管道，日志文件不再出现
    JSON 与气泡粘行的废数据。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, OSError, ValueError):
            pass  # 非标准流（pytest capsys 等）没有 reconfigure，保持原样
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=_StderrFactory(_audit_file()),
        wrapper_class=structlog.make_filtering_bound_logger(_log_level()),
    )


__all__ = ["configure_logging"]
