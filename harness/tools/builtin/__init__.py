"""内置工具集：开箱即用的智能体基础工具。

模块划分：
- base:  时间、安全计算器（AST 白名单，非 eval）
- fs:    文件读写、列目录、文本搜索
- shell: 命令执行、Python 脚本执行（argv 列表，无 shell）
- web:   HTTP 请求（scheme 白名单、响应截断）

一键注册：

    from harness.tools.registry import ToolRegistry
    from harness.tools.builtin import register_builtin

    tools = ToolRegistry()
    register_builtin(tools)
"""

from harness.tools.builtin import base, fs, shell, web
from harness.tools.registry import ToolRegistry

__all__ = ["register_builtin"]


def register_builtin(registry: ToolRegistry, include_dangerous: bool = True) -> list[str]:
    """注册全部内置工具。

    Args:
        registry: 目标工具注册表
        include_dangerous: 是否包含危险工具（run_command/write_file/python_script）。
            为 False 时跳过 dangerous 工具，适合只读场景。

    Returns:
        实际注册的工具名列表
    """
    names: list[str] = []
    for module in (base, fs, shell, web):
        for value in vars(module).values():
            spec = getattr(value, "__tool_spec__", None)
            if spec is None or spec["name"] in names:
                continue
            if spec["dangerous"] and not include_dangerous:
                continue
            registry.register_function(value)
            names.append(spec["name"])
    return names
