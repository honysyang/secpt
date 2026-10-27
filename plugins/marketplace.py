"""插件市场：第三方工具和 Agent 插件。

支持：
- 插件发现和加载
- 插件注册和卸载
- 插件依赖管理
- 沙箱执行
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class Plugin(ABC):
    """插件基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """插件名称。"""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """插件版本。"""
        ...

    @property
    def description(self) -> str:
        """插件描述。"""
        return ""

    @abstractmethod
    async def initialize(self, context: dict[str, Any]) -> None:
        """初始化插件。

        Args:
            context: 上下文（包含注册表、配置等）
        """
        ...

    async def cleanup(self) -> None:
        """清理资源。"""
        pass


class ToolPlugin(Plugin):
    """工具插件：注册自定义工具。"""

    @abstractmethod
    def get_tools(self) -> list[dict]:
        """获取工具定义。

        Returns:
            工具定义列表
        """
        ...


class AgentPlugin(Plugin):
    """Agent 插件：注册自定义 Agent。"""

    @abstractmethod
    def get_agents(self) -> list[dict]:
        """获取 Agent 定义。

        Returns:
            Agent 定义列表
        """
        ...


class SkillPlugin(Plugin):
    """技能插件：注册自定义技能。"""

    @abstractmethod
    def get_skills(self) -> list[dict]:
        """获取技能定义。

        Returns:
            技能定义列表
        """
        ...


@dataclass
class PluginMetadata:
    """插件元数据。"""

    name: str
    version: str
    description: str
    author: str = ""
    dependencies: list[str] = field(default_factory=list)
    entry_point: str = ""  # Python 模块路径
    plugin_type: str = "tool"  # tool / agent / skill


@dataclass
class LoadedPlugin:
    """已加载的插件。"""

    metadata: PluginMetadata
    instance: Plugin
    loaded_at: float = field(default_factory=lambda: __import__("time").time())


class PluginMarketplace:
    """插件市场：发现、加载、管理插件。"""

    def __init__(self, plugins_dir: str | Path = "plugins/") -> None:
        """初始化。

        Args:
            plugins_dir: 插件目录
        """
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        self._loaded: dict[str, LoadedPlugin] = {}
        self._registry: dict[str, Any] = {}  # 工具/Agent/技能注册表

    def discover(self) -> list[PluginMetadata]:
        """发现可用插件。

        Returns:
            插件元数据列表
        """
        plugins = []

        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            # 查找 plugin.json 或 plugin.py
            metadata_file = plugin_dir / "plugin.json"
            init_file = plugin_dir / "__init__.py"

            if metadata_file.exists():
                import json
                with open(metadata_file) as f:
                    data = json.load(f)
                plugins.append(PluginMetadata(**data))
            elif init_file.exists():
                # 从模块推断
                plugins.append(PluginMetadata(
                    name=plugin_dir.name,
                    version="0.1.0",
                    description=f"Plugin from {plugin_dir.name}",
                    entry_point=plugin_dir.name,
                ))

        return plugins

    async def load(self, plugin_name: str, context: dict | None = None) -> Plugin:
        """加载插件。

        Args:
            plugin_name: 插件名称
            context: 初始化上下文

        Returns:
            插件实例

        Raises:
            ValueError: 插件不存在
        """
        if plugin_name in self._loaded:
            return self._loaded[plugin_name].instance

        # 查找插件
        plugin_path = self.plugins_dir / plugin_name
        if not plugin_path.exists():
            raise ValueError(f"Plugin not found: {plugin_name}")

        # 动态导入
        sys.path.insert(0, str(self.plugins_dir))
        try:
            module = importlib.import_module(plugin_name)

            # 查找 Plugin 子类
            plugin_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, Plugin) and attr != Plugin:
                    plugin_class = attr
                    break

            if plugin_class is None:
                raise ValueError(f"No Plugin subclass found in {plugin_name}")

            # 实例化
            instance = plugin_class()
            await instance.initialize(context or {})

            # 记录
            metadata = PluginMetadata(
                name=instance.name,
                version=instance.version,
                description=instance.description,
                entry_point=plugin_name,
            )
            self._loaded[plugin_name] = LoadedPlugin(metadata, instance)

            return instance

        finally:
            sys.path.remove(str(self.plugins_dir))

    async def unload(self, plugin_name: str) -> bool:
        """卸载插件。

        Args:
            plugin_name: 插件名称

        Returns:
            是否成功
        """
        if plugin_name not in self._loaded:
            return False

        plugin = self._loaded[plugin_name]
        await plugin.instance.cleanup()
        del self._loaded[plugin_name]
        return True

    def get_loaded_plugins(self) -> list[PluginMetadata]:
        """获取已加载插件列表。"""
        return [p.metadata for p in self._loaded.values()]

    def get_plugin(self, plugin_name: str) -> Plugin | None:
        """获取已加载的插件实例。"""
        if loaded := self._loaded.get(plugin_name):
            return loaded.instance
        return None


# 示例插件实现
class ExampleToolPlugin(ToolPlugin):
    """示例工具插件。"""

    @property
    def name(self) -> str:
        return "example_tools"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "示例工具插件"

    async def initialize(self, context: dict[str, Any]) -> None:
        """初始化：注册工具。"""
        if "tool_registry" in context:
            registry = context["tool_registry"]
            for tool in self.get_tools():
                registry.register(**tool)

    def get_tools(self) -> list[dict]:
        """获取工具列表。"""
        return [
            {
                "name": "example_tool",
                "func": lambda x: f"Example: {x}",
                "description": "示例工具",
            },
        ]
