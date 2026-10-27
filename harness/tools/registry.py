"""工具协议：带 Schema、异步支持、错误处理的工具注册表。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    """工具定义。

    Attributes:
        name: 工具名称
        description: 工具描述（用于 LLM 理解）
        func: 工具函数（同步或异步）
        parameters: JSON Schema 格式的参数定义
        dangerous: 是否需要人工确认
    """

    name: str
    description: str
    func: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False

    @property
    def is_async(self) -> bool:
        """是否为异步函数。"""
        return inspect.iscoroutinefunction(self.func)

    async def execute(self, **kwargs: Any) -> Any:
        """执行工具。"""
        if self.is_async:
            return await self.func(**kwargs)
        return self.func(**kwargs)

    def to_schema(self) -> dict[str, Any]:
        """转换为 OpenAI 工具 Schema 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


class ToolRegistry:
    """工具注册中心。

    支持：
    - 带 Schema 的工具注册
    - 异步工具执行
    - 工具分类（普通/危险）
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # 危险工具审批钩子：签名 (tool_name, arguments_json) -> bool。
        # 跟随注册表走——共享表挂一次，经 create_experts 派生的私有表自动继承
        self.approval_hook: Callable[[str, str], Any] | None = None

    def register(
        self,
        name: str,
        func: Callable[..., Any],
        description: str = "",
        parameters: dict[str, Any] | None = None,
        dangerous: bool = False,
    ) -> None:
        """注册工具。

        Args:
            name: 工具名称
            func: 工具函数（同步或异步）
            description: 工具描述
            parameters: JSON Schema 参数定义
            dangerous: 是否需要人工确认
        """
        self._tools[name] = Tool(
            name=name,
            description=description or func.__doc__ or f"工具: {name}",
            func=func,
            parameters=parameters or {},
            dangerous=dangerous,
        )

    def register_tool(self, tool_obj: Tool) -> None:
        """注册现成的 Tool 对象。

        Args:
            tool_obj: 已构造好的工具定义
        """
        self._tools[tool_obj.name] = tool_obj

    def register_function(self, func: Callable[..., Any]) -> None:
        """注册由 @tool 装饰器标记的函数。

        Args:
            func: 带有 __tool_spec__ 元数据的函数

        Raises:
            TypeError: 函数未被 @tool 装饰时抛出
        """
        spec = getattr(func, "__tool_spec__", None)
        if spec is None:
            raise TypeError(f"函数 {func.__name__!r} 未被 @tool 装饰，无法注册")
        self.register(
            name=spec["name"],
            func=func,
            description=spec["description"],
            parameters=spec["parameters"],
            dangerous=spec["dangerous"],
        )

    def get(self, name: str) -> Tool | None:
        """获取工具。"""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """列出所有工具名。"""
        return list(self._tools.keys())

    def list_schemas(self) -> list[dict[str, Any]]:
        """列出所有工具的 Schema。"""
        return [tool.to_schema() for tool in self._tools.values()]

    def get_dangerous_tools(self) -> list[str]:
        """获取需要人工确认的工具列表。"""
        return [name for name, tool in self._tools.items() if tool.dangerous]


# 全局注册表
registry = ToolRegistry()
