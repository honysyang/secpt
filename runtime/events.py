"""事件总线：模块间通信的解耦机制。

支持：
- 同步/异步事件监听
- 事件过滤
- 一次性监听
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Event:
    """事件对象。"""

    name: str
    data: dict[str, Any]
    source: str = ""


Listener = Callable[[Event], Any]


class EventBus:
    """事件总线。"""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}
        self._once_listeners: dict[str, list[Listener]] = {}
        self._history: list[Event] = []

    def on(self, event_name: str, listener: Listener) -> None:
        """订阅事件。"""
        self._listeners.setdefault(event_name, []).append(listener)

    def once(self, event_name: str, listener: Listener) -> None:
        """订阅一次性事件。"""
        self._once_listeners.setdefault(event_name, []).append(listener)

    def off(self, event_name: str, listener: Listener) -> None:
        """取消订阅。"""
        if event_name in self._listeners:
            self._listeners[event_name] = [
                l for l in self._listeners[event_name] if l != listener
            ]

    async def emit(self, event_name: str, data: dict[str, Any] | None = None,
                   source: str = "") -> None:
        """触发事件。

        Args:
            event_name: 事件名
            data: 事件数据
            source: 事件来源
        """
        event = Event(name=event_name, data=data or {}, source=source)
        self._history.append(event)

        # 普通监听
        for listener in self._listeners.get(event_name, []):
            await self._invoke(listener, event)

        # 一次性监听
        once_listeners = self._once_listeners.pop(event_name, [])
        for listener in once_listeners:
            await self._invoke(listener, event)

    async def _invoke(self, listener: Listener, event: Event) -> None:
        """调用监听器，支持同步和异步。"""
        import inspect

        result = listener(event)
        if inspect.isawaitable(result):
            await result

    def get_history(self, event_name: str | None = None) -> list[Event]:
        """获取事件历史。"""
        if event_name:
            return [e for e in self._history if e.name == event_name]
        return self._history.copy()


# 全局事件总线
bus = EventBus()
