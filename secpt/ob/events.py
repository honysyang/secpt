"""事件总线桩（W1-3：事件线不实现，只立桩）。

为什么现在就建：§8 钉死"事件流全部由 store/loop 触发，ob/* 只读订阅"——小票写入
触发 ticket_appended 这类刻点必须从第一天就有名字，否则 M2 接 web 面板时要把
store/loop 翻一遍补埋点。M1 的总线只做两件事：structlog 落一行 + 通知订阅者。

订阅者异常绝不冒泡：可观测性是旁观者，旁观者炸不能停主循环（循环悬死是 §9 明令
禁止的状态）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Literal

import structlog

_log = structlog.get_logger()
_std_log = logging.getLogger(__name__)

EventName = Literal[
    "run_step_started",
    "ticket_appended",
    "exit_passed",
    "exit_failed",
    "run_ended",
]

Subscriber = Callable[[str, Mapping[str, object]], None]


class EventBus:
    """极小发布/订阅。emit 的语义 = "这件事发生了"，订阅者自己决定怎么消化。"""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        """注册订阅者；重复注册同一函数会收到多份事件（幂等由调用方自查）。"""
        self._subscribers.append(subscriber)

    def emit(self, name: str, /, **payload: object) -> None:
        """发一条事件：structlog 结构化落一行 + 逐个通知订阅者（异常只告警不冒泡）。"""
        fields: dict[str, object] = {"event": name, **payload}
        # structlog 的第一个位置参数就是 event，不能在 kwargs 里再传同名键
        _log.info(name, **{k: v for k, v in fields.items() if k != "event"})
        for subscriber in list(self._subscribers):
            try:
                subscriber(name, fields)
            except Exception:
                _std_log.exception("event subscriber failed; 观测不得干扰主循环")


__all__ = ["EventName", "EventBus", "Subscriber"]
