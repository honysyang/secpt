"""角色配置测试：RoleSpec、RoleRegistry、内置模板、JSON 配置加载。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.tools.builtin import register_builtin
from harness.tools.registry import ToolRegistry
from runtime.agents.roles import (
    DuplicateRoleError,
    RoleError,
    RoleNotFoundError,
    RoleRegistry,
    RoleSpec,
)


class FakeLLM:
    """最小 LLM 替身。"""

    async def chat(self, messages, tools=None):
        return {"choices": [{"message": {"content": "ok"}}]}


class TestRoleSpec:
    """角色声明测试。"""

    def test_from_dict_valid(self):
        spec = RoleSpec.from_dict("coder", {
            "instructions": "写代码",
            "tools": ["read_file"],
            "description": "编码",
            "max_steps": 5,
        })
        assert spec.name == "coder"
        assert spec.instructions == "写代码"
        assert spec.tools == ["read_file"]
        assert spec.max_steps == 5

    def test_from_dict_missing_instructions(self):
        with pytest.raises(RoleError, match="instructions"):
            RoleSpec.from_dict("bad", {"tools": []})

    def test_from_dict_not_object(self):
        with pytest.raises(RoleError, match="必须是对象"):
            RoleSpec.from_dict("bad", "string")  # type: ignore[arg-type]

    def test_from_dict_ignores_unknown_fields(self):
        spec = RoleSpec.from_dict("a", {"instructions": "x", "future_field": 1})
        assert spec.instructions == "x"


class TestRoleRegistry:
    """注册表行为测试。"""

    def test_register_and_get(self):
        registry = RoleRegistry()
        spec = RoleSpec(name="dev", instructions="开发")
        registry.register(spec)
        assert registry.get("dev") is spec

    def test_duplicate_registration_raises(self):
        registry = RoleRegistry()
        registry.register(RoleSpec(name="dev", instructions="a"))
        with pytest.raises(DuplicateRoleError):
            registry.register(RoleSpec(name="dev", instructions="b"))

    def test_override_allowed(self):
        registry = RoleRegistry()
        registry.register(RoleSpec(name="dev", instructions="a"))
        registry.register(RoleSpec(name="dev", instructions="b"), override=True)
        assert registry.get("dev").instructions == "b"

    def test_get_missing_raises_with_available(self):
        registry = RoleRegistry()
        registry.register(RoleSpec(name="a", instructions="x"))
        with pytest.raises(RoleNotFoundError, match="'a'"):
            registry.get("nope")

    def test_list_roles(self):
        registry = RoleRegistry()
        registry.register(RoleSpec(name="a", instructions="x", description="A 角色"))
        assert registry.list_roles() == {"a": "A 角色"}


class TestBuiltinRoles:
    """内置模板库测试。"""

    def test_with_builtins_contains_core_roles(self):
        registry = RoleRegistry.with_builtins()
        roles = registry.list_roles()
        for expected in ("planner", "coordinator", "researcher", "coder", "reviewer", "writer", "analyst"):
            assert expected in roles, f"缺少内置角色 {expected}"

    def test_builtin_roles_have_instructions(self):
        for spec in RoleRegistry.with_builtins()._roles.values():
            assert spec.instructions.strip(), f"角色 {spec.name} 缺少 instructions"


class TestCreateAgent:
    """工厂构造测试。"""

    @pytest.fixture
    def shared_tools(self) -> ToolRegistry:
        reg = ToolRegistry()
        register_builtin(reg)
        return reg

    def test_coder_gets_declared_tools_only(self, shared_tools: ToolRegistry):
        registry = RoleRegistry.with_builtins()
        agent = registry.create_agent("coder", FakeLLM(), shared_tools=shared_tools)

        assert agent.name == "coder"
        # coder 声明了工具 → 私有工具集且是共享池的子集
        declared = set(registry.get("coder").tools)
        assert declared and declared <= set(shared_tools.list_tools())
        # 最小权限：reviewer 有 read_file 但 coder 不一定有 http_request
        assert "http_request" not in agent.tools.list_tools()

    def test_writer_is_toolless(self):
        registry = RoleRegistry.with_builtins()
        agent = registry.create_agent("writer", FakeLLM(), shared_tools=ToolRegistry())
        assert agent.tools.list_tools() == []

    def test_missing_tool_warns_and_skips(self, shared_tools: ToolRegistry):
        """声明了不存在的工具：告警跳过，不中断构造（静默降级）。"""
        registry = RoleRegistry()
        registry.register(RoleSpec(
            name="custom",
            instructions="x",
            tools=["read_file", "no_such_tool"],
        ))
        agent = registry.create_agent("custom", FakeLLM(), shared_tools=shared_tools)
        assert agent.tools.list_tools() == ["read_file"]

    def test_unknown_role_raises(self):
        with pytest.raises(RoleNotFoundError):
            RoleRegistry.with_builtins().create_agent("ghost", FakeLLM())


class TestExpertPool:
    """全量专家池：配置新增角色自动可被路由（回归：pentest_recon 曾进不了池）。"""

    def test_create_agent_carries_description(self):
        registry = RoleRegistry.with_builtins()
        agent = registry.create_agent("coder", FakeLLM())
        assert agent.description == registry.get("coder").description

    def test_create_experts_includes_config_roles(self, tmp_path: Path):
        config = tmp_path / "roles.json"
        config.write_text(json.dumps({"roles": {
            "pentest_recon": {"instructions": "侦察", "description": "安全侦察"},
        }}), encoding="utf-8")

        registry = RoleRegistry.with_builtins()
        registry.load_file(config)
        experts = registry.create_experts(FakeLLM())

        # 内置 + 配置角色全在池中，协调者被排除
        assert "pentest_recon" in experts
        assert "coder" in experts
        assert "coordinator" not in experts

    def test_create_experts_respects_exclude(self):
        registry = RoleRegistry.with_builtins()
        experts = registry.create_experts(FakeLLM(), exclude=("coordinator", "planner"))
        assert "planner" not in experts
        assert "coordinator" not in experts


class TestRouterPromptQuality:
    """路由提示必须带专家职责描述——角色池变大后仅凭名字无法路由。"""

    @pytest.mark.asyncio
    async def test_route_prompt_contains_descriptions(self):
        from runtime.agents.base import SubAgent
        from runtime.agents.collaboration import Router

        captured: list[str] = []

        class RecordingLLM(FakeLLM):
            async def chat(self, messages, tools=None):
                captured.append(messages[-1]["content"])
                return {"choices": [{"message": {"content": "coder"}}]}

        def make(name: str, desc: str) -> SubAgent:
            return SubAgent(name=name, instructions="x", llm=RecordingLLM(), description=desc)

        router = Router(
            coordinator=make("coordinator", "协调"),
            sub_agents={
                "coder": make("coder", "代码实现与修复"),
                "pentest_recon": make("pentest_recon", "安全侦察"),
            },
        )
        await router.route("扫描网站找漏洞")

        prompt = captured[0]
        assert "coder: 代码实现与修复" in prompt
        assert "pentest_recon: 安全侦察" in prompt


class TestConfigFile:
    """JSON 配置文件加载测试。"""

    def _write_config(self, tmp_path: Path, roles: dict) -> Path:
        config = tmp_path / "roles.json"
        config.write_text(json.dumps({"roles": roles}), encoding="utf-8")
        return config

    def test_load_file_merges(self, tmp_path: Path):
        config = self._write_config(tmp_path, {
            "security": {"instructions": "安全专家", "tools": ["http_request"]},
        })
        registry = RoleRegistry.with_builtins()
        loaded = registry.load_file(config)

        assert loaded == ["security"]
        assert "security" in registry.list_roles()
        assert "coder" in registry.list_roles()  # 内置不受影响

    def test_load_file_overrides_builtin(self, tmp_path: Path):
        config = self._write_config(tmp_path, {
            "coder": {"instructions": "自定义 coder 提示词", "tools": []},
        })
        registry = RoleRegistry.with_builtins()
        registry.load_file(config)

        spec = registry.get("coder")
        assert spec.instructions == "自定义 coder 提示词"
        assert spec.tools == []

    def test_from_file_isolated(self, tmp_path: Path):
        """from_file 创建独立注册表，不含内置角色。"""
        config = self._write_config(tmp_path, {
            "solo": {"instructions": "独立角色"},
        })
        registry = RoleRegistry.from_file(config)
        assert registry.list_roles() == {"solo": ""}

    def test_load_invalid_file_raises(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"no_roles_key": {}}), encoding="utf-8")
        with pytest.raises(RoleError, match="roles"):
            RoleRegistry.from_file(bad)

    def test_load_project_configs(self):
        """项目自带的 configs/roles.json 必须合法（防止配置腐化）。"""
        config = Path("configs/roles.json")
        if not config.exists():
            pytest.skip("configs/roles.json 不存在")
        registry = RoleRegistry.with_builtins()
        loaded = registry.load_file(config)
        assert loaded  # 至少加载了一个角色
