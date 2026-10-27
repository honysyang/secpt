"""应用引导：LLM、工具注册表、角色注册表的共享构造逻辑。

CLI 与 Web 服务器共用，保证两种入口的工具集与角色池行为一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.tools.registry import ToolRegistry


def create_llm(provider: str | None = None) -> Any:
    """创建 LLM 客户端（真实提供商或 mock）。"""
    from adapters.llm.factory import build_llm

    return build_llm(provider)


def build_tool_registry(load_script_tools: bool = True) -> ToolRegistry:
    """构建工具注册表：内置工具 + 可选的 tools/ 目录脚本工具。"""
    from harness.tools.builtin import register_builtin

    registry = ToolRegistry()
    register_builtin(registry)
    if load_script_tools:
        from harness.tools.loader import ScriptToolLoader

        ScriptToolLoader("tools").load_into(registry)
    return registry


def load_roles(path: str | Path | None = None) -> Any:
    """加载角色注册表：内置模板 + 可选 roles.json 自定义/覆盖。

    Args:
        path: 配置文件路径，默认 configs/roles.json（Web 热重载可传自定义路径）
    """
    from runtime.agents.roles import RoleRegistry

    roles = RoleRegistry.with_builtins()
    roles_file = Path(path) if path else Path("configs/roles.json")
    if roles_file.exists():
        roles.load_file(roles_file)
    return roles
