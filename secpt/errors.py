"""分层异常体系（开发文档 §9）。

约定：库层不吞错、统一上抛；每个异常自带 ``code`` 与 ``context``，便于 audit_log 落账与
主循环（main.py 兜底）把它们分派到正确的异常出口分支。

为什么这样设计：错误矩阵里每一类错误对应不同的"落账 + 出口"语义（E_SCHEMA 重试、
E_BUDGET_EXHAUSTED 停执行等）。用类型分层而不是字符串匹配，可以让主循环的分支
写成 ``except LlmError`` 这种可单测的结构，也让新错误类不会因为漏登记而静默降级
成通用 Exception。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class SecptError(Exception):
    """全部自研异常的基类：code + context 双字段，落账语义在子类层细化。"""

    code: str = "E_SECPT"

    def __init__(self, message: str, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        """供 audit_log/日志直接使用的结构化表示。"""
        return {"code": self.code, "message": str(self), "context": self.context}


class LedgerError(SecptError):
    """事实层错误：非法写入、schema 违反、越权直写 sqlite（摸到 append-only 红线等）。"""

    code = "E_LEDGER"
