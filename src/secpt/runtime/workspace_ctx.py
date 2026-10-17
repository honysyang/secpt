"""工具执行上下文：当前工作区（供插件工具读写 ws/ 产物）。

Engine.run 在每个任务开始时 set，结束时 reset。用 ContextVar 而非环境变量：
frontier 多 worker 在各自线程里跑 engine，环境变量是进程级会互踩；ContextVar
随 asyncio.to_thread 的上下文复制语义隔离到每个任务。
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar, Token

_WORK_DIR: ContextVar[str] = ContextVar("secpt_work_dir", default="")


def set_work_dir(path: object) -> Token:
    return _WORK_DIR.set(str(path))


def reset_work_dir(token: Token) -> None:
    # 跨上下文 reset 失败不阻断收尾
    with contextlib.suppress(ValueError, RuntimeError):
        _WORK_DIR.reset(token)


def current_work_dir() -> str:
    """当前引擎工作区（未在 Engine 内运行时为空串）。"""
    return _WORK_DIR.get("")
