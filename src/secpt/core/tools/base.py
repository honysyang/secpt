"""工具抽象与注册表。

约束：
- :class:`Tool` 是 async 契约，``run(args) -> str`` 不抛（内部自行 try/except 后返回 "Error: ..."）
- :class:`Registry` 仅是内存字典，不做序列化
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Tool(ABC):
    """可调用工具的抽象基类。"""

    name: str = ""
    description: str = ""

    @abstractmethod
    async def run(self, args: str) -> str:
        """执行工具并返回字符串结果（或 ``"Error: ..."``）。"""
        raise NotImplementedError


class Registry:
    """工具注册表（name → Tool）。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具；重名直接覆盖（Phase 1 简化）。"""
        if not tool.name:
            raise ValueError("Tool.name 不能为空")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名查找；未找到返回 None。"""
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        """返回所有工具（按名字典序，稳定输出）。"""
        return [self._tools[k] for k in sorted(self._tools)]

    def names(self) -> list[str]:
        """返回所有工具名（字典序）。"""
        return sorted(self._tools)


__all__ = ["Registry", "Tool"]
