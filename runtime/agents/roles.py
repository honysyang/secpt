"""角色配置：声明式定义智能体角色，代码外的数据驱动配置。

设计动机：
- 角色三要素（instructions / tools / max_steps）收敛到 RoleSpec，
  SubAgent 从 spec 工厂化构造，新增角色不再改业务代码
- 内置模板开箱即用，JSON 配置文件可覆盖/扩展（数据驱动配置的架构约定）
- create_agent 按角色声明过滤共享工具池，实现"每个角色最小权限工具集"

配置文件示例（configs/roles.json）：

    {
        "roles": {
            "coder": {
                "instructions": "你是资深工程师...",
                "tools": ["read_file", "write_file", "run_command"],
                "description": "代码实现"
            }
        }
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.logging.logger import get_logger
from harness.tools.registry import ToolRegistry
from runtime.agents.base import SubAgent

logger = get_logger(__name__)


class RoleError(Exception):
    """角色配置基础异常。"""


class DuplicateRoleError(RoleError):
    """角色重复注册。"""


class RoleNotFoundError(RoleError):
    """角色不存在。"""


@dataclass
class RoleSpec:
    """角色声明。

    Attributes:
        name: 角色名（唯一标识，协作模式用它路由）
        instructions: 系统提示词（角色职责与行为约束）
        description: 一句话描述（供人阅读与 LLM 路由参考）
        tools: 允许使用的工具名列表；空列表表示无工具的纯文本角色
        max_steps: ReAct 循环最大步数
    """

    name: str
    instructions: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    max_steps: int = 10

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> RoleSpec:
        """从配置字典构造，忽略未知字段（前向兼容）。"""
        if not isinstance(data, dict):
            raise RoleError(f"角色 {name!r} 的配置必须是对象")
        if not data.get("instructions"):
            raise RoleError(f"角色 {name!r} 缺少 instructions")
        return cls(
            name=name,
            instructions=str(data["instructions"]),
            description=str(data.get("description", "")),
            tools=[str(t) for t in data.get("tools", [])],
            max_steps=int(data.get("max_steps", 10)),
        )


# ---------- 内置角色模板库 ----------

BUILTIN_ROLES: list[RoleSpec] = [
    RoleSpec(
        name="planner",
        instructions=(
            "你是任务规划师。把复杂任务拆解为有序、可执行的子任务清单，"
            "每步明确产出物与负责人角色。只输出计划，不执行。"
        ),
        description="任务拆解与计划",
    ),
    RoleSpec(
        name="coordinator",
        instructions=(
            "你是协调员。分析用户任务，判断应该路由给哪位专家处理，"
            "只输出专家名称本身，不要输出其他内容。"
        ),
        description="多智能体路由协调",
    ),
    RoleSpec(
        name="researcher",
        instructions=(
            "你是调研员。善于收集、核对与归纳信息，"
            "引用来源并标注可信度，区分事实与推测。"
        ),
        description="信息调研与归纳",
        tools=["read_file", "list_dir", "search_text", "http_request"],
    ),
    RoleSpec(
        name="coder",
        instructions=(
            "你是资深工程师。编写简洁、健壮、可测试的代码；"
            "修改前先读相关文件；完成后说明变更点与风险。"
        ),
        description="代码实现与修复",
        tools=["read_file", "write_file", "search_text", "run_command", "python_script"],
    ),
    RoleSpec(
        name="reviewer",
        instructions=(
            "你是代码审查者。从正确性、安全性、可读性三个维度审查，"
            "每个问题给出严重级别与修改建议，不做无关重构。"
        ),
        description="代码与方案审查",
        tools=["read_file", "search_text"],
    ),
    RoleSpec(
        name="writer",
        instructions=(
            "你是技术写手。输出结构清晰、受众明确的文档与文案，"
            "先列提纲再展开，避免空话。"
        ),
        description="文档与文案撰写",
    ),
    RoleSpec(
        name="analyst",
        instructions=(
            "你是数据分析师。先澄清问题与口径，再计算和解释，"
            "结论必须附带推导依据。"
        ),
        description="数据计算与分析",
        tools=["calculate", "read_file", "http_request"],
    ),
]


class RoleRegistry:
    """角色注册表：管理 RoleSpec 并工厂化构造 SubAgent。"""

    def __init__(self) -> None:
        self._roles: dict[str, RoleSpec] = {}

    @classmethod
    def with_builtins(cls) -> RoleRegistry:
        """创建含内置模板库的注册表。"""
        registry = cls()
        for spec in BUILTIN_ROLES:
            registry.register(spec)
        return registry

    def register(self, spec: RoleSpec, override: bool = False) -> None:
        """注册角色。

        Args:
            spec: 角色声明
            override: 同名角色是否覆盖（配置文件加载时为 True）

        Raises:
            DuplicateRoleError: 未允许覆盖且角色已存在
        """
        if spec.name in self._roles and not override:
            raise DuplicateRoleError(f"角色 {spec.name!r} 已注册")
        self._roles[spec.name] = spec

    def get(self, name: str) -> RoleSpec:
        """获取角色声明。

        Raises:
            RoleNotFoundError: 角色不存在
        """
        if name not in self._roles:
            raise RoleNotFoundError(
                f"角色 {name!r} 不存在，可用角色: {sorted(self._roles)}"
            )
        return self._roles[name]

    def list_roles(self) -> dict[str, str]:
        """列出所有角色 {name: description}。"""
        return {name: spec.description for name, spec in self._roles.items()}

    def list_specs(self) -> dict[str, RoleSpec]:
        """列出所有角色完整声明（供配置中心展示/编辑）。"""
        return dict(self._roles)

    def load_file(self, path: str | Path) -> list[str]:
        """从 JSON 配置文件合并加载角色（同名覆盖内置）。

        Args:
            path: 配置文件路径

        Returns:
            实际加载的角色名列表
        """
        file_path = Path(path)
        data = json.loads(file_path.read_text(encoding="utf-8"))
        roles = data.get("roles")
        if not isinstance(roles, dict):
            raise RoleError(f"配置文件 {file_path} 缺少 roles 对象")

        loaded: list[str] = []
        for name, role_data in roles.items():
            spec = RoleSpec.from_dict(name, role_data)
            self.register(spec, override=True)
            loaded.append(name)

        logger.info("roles_loaded_from_file", file=str(file_path), roles=loaded)
        return loaded

    @classmethod
    def from_file(cls, path: str | Path) -> RoleRegistry:
        """从配置文件创建独立注册表（不含内置角色）。"""
        registry = cls()
        registry.load_file(path)
        return registry

    def create_agent(
        self,
        name: str,
        llm: Any,
        shared_tools: ToolRegistry | None = None,
    ) -> SubAgent:
        """按角色声明构造 SubAgent。

        工具策略：spec.tools 非空时，从共享工具池按名单过滤构造
        私有 ToolRegistry（角色最小权限）；名单中不存在的工具记录
        告警并跳过，不中断构造（可降级）。

        Args:
            name: 角色名
            llm: LLM 客户端
            shared_tools: 共享工具池（通常为内置工具注册表）

        Returns:
            配置好的 SubAgent 实例
        """
        spec = self.get(name)

        tools: ToolRegistry | None = None
        if spec.tools:
            tools = ToolRegistry()
            # 危险工具审批钩子随注册表继承，保证专家与主循环同一审批策略
            if shared_tools is not None and shared_tools.approval_hook is not None:
                tools.approval_hook = shared_tools.approval_hook
            for tool_name in spec.tools:
                tool_obj = shared_tools.get(tool_name) if shared_tools else None
                if tool_obj is None:
                    logger.warning(
                        "role_tool_missing", role=name, tool=tool_name
                    )
                    continue
                tools.register_tool(tool_obj)

        return SubAgent(
            name=spec.name,
            instructions=spec.instructions,
            llm=llm,
            tools=tools,
            max_steps=spec.max_steps,
            description=spec.description,
        )

    def create_experts(
        self,
        llm: Any,
        shared_tools: ToolRegistry | None = None,
        exclude: tuple[str, ...] = ("coordinator",),
    ) -> dict[str, SubAgent]:
        """把注册表中全部角色构造为路由候选池（排除协调类角色）。

        解决"配置文件新增角色进不了协作路由"的集成缺口：
        CLI/编排层不再硬编码专家名单，注册表里有什么就能路由什么。

        Args:
            llm: LLM 客户端
            shared_tools: 共享工具池
            exclude: 排除的角色名（协调者自身不作为路由目标）

        Returns:
            {角色名: SubAgent} 专家池
        """
        return {
            name: self.create_agent(name, llm, shared_tools=shared_tools)
            for name in self._roles
            if name not in exclude
        }
