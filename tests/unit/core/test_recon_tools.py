"""真实工具（nmap/curl/dig）测试。

要求系统已安装 nmap/curl/dig；未安装时 skip。
"""

from __future__ import annotations

import shutil

import pytest

from secpt.config.models import ToolsConfig
from secpt.core.tools.registry import build_registry
from secpt.runtime import subprocess as sp

HAS_NMAP = shutil.which("nmap") is not None
HAS_CURL = shutil.which("curl") is not None
HAS_DIG = shutil.which("dig") is not None


@pytest.fixture
def full_whitelist(monkeypatch: pytest.MonkeyPatch) -> ToolsConfig:
    """放行 echo/nmap/curl/dig。"""
    cfg = ToolsConfig(whitelist=["echo", "nmap", "curl", "dig"])
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)
    return cfg


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_NMAP, reason="nmap not installed")
async def test_nmap_tool(full_whitelist: ToolsConfig) -> None:
    from secpt.tools.recon import NmapTool

    tool = NmapTool()
    out = await tool.run("-p 22,80 -T4 127.0.0.1")
    assert "Error" not in out[:20]  # 不应立刻报错
    assert out.strip()


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_CURL, reason="curl not installed")
async def test_curl_tool(full_whitelist: ToolsConfig) -> None:
    from secpt.tools.recon import CurlTool

    tool = CurlTool()
    out = await tool.run("-sS -o /dev/null -w '%{http_code}' http://127.0.0.1:1/")
    # 连接拒绝也会返回 000 或错误码，但不应是 "Error: ..."
    assert "Error" not in out[:20]


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_DIG, reason="dig not installed")
async def test_dig_tool(full_whitelist: ToolsConfig) -> None:
    from secpt.tools.recon import DigTool

    tool = DigTool()
    out = await tool.run("localhost A")
    assert "Error" not in out[:20]
    assert out.strip()


def test_build_registry_selects_whitelist() -> None:
    reg = build_registry(tool_names=["echo", "nmap"])
    names = reg.names()
    assert "echo" in names
    assert "nmap" in names
    assert "curl" not in names
    assert "dig" not in names


def test_build_registry_empty_tool_names() -> None:
    reg = build_registry(tool_names=[])
    assert reg.names() == []
