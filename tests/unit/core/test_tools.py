"""工具注册表与 EchoTool 测试。"""

from __future__ import annotations

import pytest

from secpt.config.models import ToolsConfig
from secpt.core.tools.base import Registry, Tool
from secpt.core.tools.demo import EchoTool
from secpt.runtime import subprocess as sp


class _DummyTool(Tool):
    name = "dummy"
    description = "dummy tool"

    async def run(self, args: str) -> str:
        return f"dummy:{args}"


def test_registry_register_get() -> None:
    reg = Registry()
    reg.register(_DummyTool())
    assert reg.get("dummy") is not None
    assert reg.get("not-exist") is None


def test_registry_names_and_all() -> None:
    reg = Registry()
    reg.register(EchoTool())
    reg.register(_DummyTool())
    assert reg.names() == ["dummy", "echo"]
    assert [t.name for t in reg.all()] == ["dummy", "echo"]


def test_registry_reject_empty_name() -> None:
    class _NoName(Tool):
        name = ""
        description = "x"

        async def run(self, args: str) -> str:
            return ""

    with pytest.raises(ValueError, match="name"):
        Registry().register(_NoName())


@pytest.fixture
def echo_config(monkeypatch: pytest.MonkeyPatch) -> ToolsConfig:
    """放行 echo 的 ToolsConfig，monkeypatch 到 subprocess.get_settings。"""
    cfg = ToolsConfig(whitelist=["echo"])
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)
    return cfg


@pytest.mark.asyncio
async def test_echo_tool_runs(echo_config: ToolsConfig) -> None:
    """EchoTool 在 ToolsConfig.whitelist=['echo'] 下能跑。"""
    tool = EchoTool()
    out = await tool.run("hi")
    assert "hi" in out


@pytest.mark.asyncio
async def test_echo_tool_error_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """EchoTool 不在白名单时返回 'Error: ...' 字符串，不抛。"""
    cfg = ToolsConfig(whitelist=[])
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)

    tool = EchoTool()
    out = await tool.run("hi")
    assert out.startswith("Error:")
    assert "echo" in out
