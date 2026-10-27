"""@tool 装饰器：从函数签名与 docstring 自动生成工具 Schema。

设计动机：
- 让用户"写一个普通函数"即可成为智能体工具，无需手写 JSON Schema
- Schema 来源单一且贴近代码：类型注解 → 参数类型，docstring → 描述
- 与 ToolRegistry 解耦：装饰器只附加元数据，注册时机由调用方决定

用法示例：

    @tool
    def get_weather(city: str, days: int = 3) -> str:
        # docstring 首段作为工具描述，Args 段作为参数说明
        return f"{city} 未来 {days} 天晴"

    @tool(name="calc", dangerous=True)
    def calculate(expression: str) -> str:
        ...
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

# Python 类型 → JSON Schema 类型的映射
_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _parse_arg_docs(doc: str | None) -> dict[str, str]:
    """解析 Google 风格 docstring 的 Args 段，返回 {参数名: 描述}。

    示例输入：
        查询天气。

        Args:
            city: 城市名
            days: 预报天数
    """
    docs: dict[str, str] = {}
    if not doc:
        return docs

    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if in_args:
            if not stripped:
                continue
            # "name: 描述" 或 "name(类型): 描述"
            if ":" in stripped and stripped[0].isalpha() or stripped.startswith("_"):
                name, _, desc = stripped.partition(":")
                docs[name.strip().split("(")[0].strip()] = desc.strip()
            elif not stripped.startswith(("Returns", "Raises", "Examples", "Note")):
                continue
            else:
                break
    return docs


def schema_from_signature(func: Callable[..., Any]) -> dict[str, Any]:
    """从函数签名生成 JSON Schema。

    规则：
    - 类型注解映射为 JSON 类型（经 get_type_hints 解析，
      兼容 `from __future__ import annotations` 的字符串注解）
    - 有默认值的参数非必填，并把默认值写入 schema（便于 LLM 参考）
    - *args/**kwargs 跳过（LLM 工具调用走具名参数）
    """
    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}  # 类型引用无法解析时退化为无类型 schema
    arg_docs = _parse_arg_docs(func.__doc__)

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in signature.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue

        prop: dict[str, Any] = {}
        annotation = hints.get(name, param.annotation)
        if param.annotation is not param.empty and annotation in _TYPE_MAP:
            prop["type"] = _TYPE_MAP[annotation]
        if name in arg_docs:
            prop["description"] = arg_docs[name]
        if param.default is not param.empty:
            prop["default"] = param.default
        else:
            required.append(name)

        properties[name] = prop

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _extract_summary(doc: str | None) -> str:
    """取 docstring 首段作为工具描述。"""
    if not doc:
        return ""
    return doc.split("\n\n")[0].strip()


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    dangerous: bool = False,
) -> Callable[..., Any]:
    """把普通函数标记为智能体工具。

    支持两种形式：
        @tool                       # 直接装饰
        @tool(name="x", dangerous=True)  # 带参数装饰

    元数据存放在 func.__tool_spec__，由 ToolRegistry.register_function 消费。
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__tool_spec__ = {  # type: ignore[attr-defined]
            "name": name or fn.__name__,
            "description": description or _extract_summary(fn.__doc__),
            "parameters": schema_from_signature(fn),
            "dangerous": dangerous,
        }
        return fn

    if func is not None:
        return decorate(func)
    return decorate
