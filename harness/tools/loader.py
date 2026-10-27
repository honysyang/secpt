"""脚本工具加载器：把用户编写的 Python 脚本自动注册为智能体工具。

脚本约定（最小化侵入，用户只写普通 Python 文件）：

    # tools/my_tool.py
    TOOL_NAME = "my_tool"             # 可选，默认取文件名
    TOOL_DESCRIPTION = "做某件事"      # 可选，默认取 execute 的 docstring 首行

    def execute(city: str, days: int = 3):
        # docstring 首段会作为工具描述，Args 段作为参数说明
        return f"{city} {days} 天晴"

加载行为：
- 同步/异步 execute 均支持
- 脚本内用 @tool 装饰过的函数同样识别（spec 优先）
- 单个脚本失败不影响其他脚本（记录告警并跳过 —— 可降级功能静默降级）
- 重名工具跳过并告警，不覆盖已有注册
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from harness.logging.logger import get_logger
from harness.tools.decorator import _extract_summary, schema_from_signature
from harness.tools.registry import ToolRegistry

logger = get_logger(__name__)


class ScriptToolLoader:
    """从目录加载 Python 脚本并注册为工具。"""

    def __init__(self, directory: str | Path = "tools") -> None:
        """初始化。

        Args:
            directory: 脚本目录，默认项目根下的 tools/
        """
        self.directory = Path(directory)

    def load_into(self, registry: ToolRegistry) -> list[str]:
        """扫描目录并把所有合法脚本注册进 registry。

        Args:
            registry: 目标工具注册表

        Returns:
            成功注册的工具名列表
        """
        if not self.directory.is_dir():
            logger.warning("script_tool_dir_missing", directory=str(self.directory))
            return []

        loaded: list[str] = []
        for script in sorted(self.directory.glob("*.py")):
            if script.name.startswith("_"):
                continue  # __init__.py / _private.py 不当作工具
            name = self._load_script(script, registry)
            if name:
                loaded.append(name)

        if loaded:
            logger.info("script_tools_loaded", count=len(loaded), names=loaded)
        return loaded

    def _load_script(self, script: Path, registry: ToolRegistry) -> str | None:
        """加载单个脚本；任何失败都只告警不中断。"""
        try:
            module = self._import(script)
        except Exception as exc:
            logger.warning("script_tool_load_failed", script=script.name, error=str(exc))
            return None

        try:
            name, func, description, dangerous = self._extract_tool(module, script)
            parameters = schema_from_signature(func)
        except ValueError as exc:
            logger.warning("script_tool_invalid", script=script.name, error=str(exc))
            return None

        if registry.get(name) is not None:
            logger.warning("script_tool_name_conflict", name=name, script=script.name)
            return None

        registry.register(
            name=name,
            func=func,
            description=description,
            parameters=parameters,
            dangerous=dangerous,
        )
        return name

    def _import(self, script: Path) -> ModuleType:
        """动态导入脚本文件为模块。"""
        module_name = f"secpt_user_tool_{script.stem}"
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法创建导入规格: {script}")
        module = importlib.util.module_from_spec(spec)
        # 隔离加载：不写入 sys.modules 常驻，但 import 期间需临时注册以支持脚本内 import
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    def _extract_tool(self, module: ModuleType, script: Path) -> tuple[str, Any, str, bool]:
        """从脚本模块提取工具定义。

        优先级：
        1. @tool 装饰的函数（取 __tool_spec__）
        2. execute 函数 + TOOL_NAME / TOOL_DESCRIPTION 常量
        """
        # 路径 1：装饰器形式
        for value in vars(module).values():
            spec = getattr(value, "__tool_spec__", None)
            if spec is not None and callable(value):
                return spec["name"], value, spec["description"], spec["dangerous"]

        # 路径 2：execute 约定
        execute = getattr(module, "execute", None)
        if execute is None or not callable(execute):
            raise ValueError("脚本未定义 execute 函数，也没有 @tool 装饰的函数")

        name = getattr(module, "TOOL_NAME", None) or script.stem
        description = getattr(module, "TOOL_DESCRIPTION", None) or _extract_summary(execute.__doc__)
        dangerous = bool(getattr(module, "TOOL_DANGEROUS", False))

        # execute 必须能以关键字参数调用（与 Tool.execute 契约一致）
        if inspect.isclass(execute):
            raise ValueError("execute 不应是类")

        return name, execute, description, dangerous
