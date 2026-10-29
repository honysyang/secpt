"""M4-2 contract test：MCP stdio 工具管道（本地 echo MCP server，零外联）。

覆盖面（任务书 M4-2 验收口径）：
- 回声回路：list_tools → manifest 形状（params/required/kind=mcp/hash）；
  call → 回声内容、raw_ref 指向真实落盘文件、int 型参数如实转换；
- 注册纪律：掉线/二进制缺失只告警不注册（返回空 adapter 列表，不炸装配）；
- 配置三档：无 [mcp] 段 = 零行为（空列表）；坏 toml / command 非字符串列表
  → E_PROFILE 拒跑；
- Guard 同权：MCP 工具过白名单闸，拒绝落 denied 回执（拒绝即不执行）；
- 失败注入：server 半途死亡 → E_TOOL_FAIL 回执（不抛错不炸主循环）；
- 清单可见：``secpt tools`` 列出 MCP 工具（manifest+hash 同一张货架）。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from secpt import main as main_mod
from secpt.errors import McpError, SecptError
from secpt.tools.guard import GuardConfig
from secpt.tools.mcp_client import (
    McpStdioClient,
    McpTool,
    build_mcp_adapters,
    load_mcp_config,
)
from secpt.tools.registry import ToolRegistry

_ECHO = str(pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "echo_mcp_server.py")


def _client() -> McpStdioClient:
    return McpStdioClient("echo", [sys.executable, _ECHO], timeout_s=10.0)


def _mcp_profile(tmp_path: pathlib.Path) -> str:
    """含 [mcp] 段的 profile：command 用 sys.executable 指向本地 echo 桩。"""
    profile = tmp_path / "profile.toml"
    profile.write_text(
        f"[[mcp.servers]]\nname = 'echo'\ncommand = ['{sys.executable}', '{_ECHO}']\n",
        encoding="utf-8",
    )
    return str(profile)


def _registry_with_echo(
    tmp_path: pathlib.Path, *, guard: GuardConfig | None = None
) -> ToolRegistry:
    client = _client()
    tools = client.list_tools()
    adapters = [McpTool(client=client, tool=t, raw_dir=tmp_path / "raw") for t in tools]
    return ToolRegistry(adapters, guard=guard)


class TestEchoRoundtrip:
    def test_list_tools_returns_manifest_shape(self, tmp_path: pathlib.Path) -> None:
        tools = _client().list_tools()
        assert [t.name for t in tools] == ["echo"]
        manifest = McpTool(client=_client(), tool=tools[0], raw_dir=tmp_path / "raw").manifest
        assert manifest.kind == "mcp"
        assert manifest.params == {"text": "str", "times": "int"}
        assert manifest.required == ("text",)
        assert manifest.hash is not None
        assert manifest.read_only is False  # 未知即写：MCP 工具默认不进 R1 重放

    def test_call_echoes_and_writes_raw(self, tmp_path: pathlib.Path) -> None:
        registry = _registry_with_echo(tmp_path)
        out = registry.dispatch("echo", {"text": "hello", "times": "2"})
        assert out.exit_code == 0
        assert out.summary == "hellohello"
        assert out.raw_ref
        raw = pathlib.Path(out.raw_ref)
        assert raw.is_file() and "hellohello" in raw.read_text(encoding="utf-8")

    def test_call_via_client_directly(self, tmp_path: pathlib.Path) -> None:
        result = _client().call("echo", {"text": "ping"})
        assert result.content_text == "ping"
        assert result.is_error is False


class TestRegistration:
    def test_build_adapters_from_profile(self, tmp_path: pathlib.Path) -> None:
        adapters = build_mcp_adapters(load_mcp_config(_mcp_profile(tmp_path)), tmp_path / "raw")
        assert [a.manifest.name for a in adapters] == ["echo"]

    def test_dead_server_skipped_not_raised(self, tmp_path: pathlib.Path) -> None:
        """掉线只告警不注册：秒退的 server 不产 adapter，装配继续。"""
        dead = tmp_path / "dead.py"
        dead.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
        cfg = load_mcp_config(str(_write_profile(tmp_path, "dead", [sys.executable, str(dead)])))
        assert build_mcp_adapters(cfg, tmp_path / "raw") == []

    def test_missing_binary_skipped_not_raised(self, tmp_path: pathlib.Path) -> None:
        cfg = load_mcp_config(str(_write_profile(tmp_path, "ghost", ["/no/such/binary"])))
        assert build_mcp_adapters(cfg, tmp_path / "raw") == []

    def test_unknown_method_maps_to_mcp_error(self, tmp_path: pathlib.Path) -> None:
        client = _client()
        with pytest.raises(McpError):
            client._request_once("no/such/method")  # 直打协议层：未知方法 → error 响应


class TestConfig:
    def test_no_section_zero_behavior(self, tmp_path: pathlib.Path) -> None:
        plain = tmp_path / "plain.toml"
        plain.write_text("target = '1.2.3.4'\n", encoding="utf-8")
        assert load_mcp_config(str(plain)) == []
        assert load_mcp_config(str(tmp_path / "missing.toml")) == []

    def test_bad_toml_rejected(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("[mcp\n", encoding="utf-8")
        with pytest.raises(SecptError) as info:
            load_mcp_config(str(bad))
        assert info.value.code == "E_PROFILE"

    def test_command_must_be_string_list(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "bad2.toml"
        bad.write_text("[[mcp.servers]]\nname = 'x'\ncommand = 'rm -rf /'\n", encoding="utf-8")
        with pytest.raises(SecptError) as info:
            load_mcp_config(str(bad))
        assert info.value.code == "E_PROFILE"


def _write_profile(tmp_path: pathlib.Path, name: str, command: list[str]) -> pathlib.Path:
    profile = tmp_path / f"profile_{name}.toml"
    argv = ", ".join(f"'{part}'" for part in command)
    profile.write_text(f"[[mcp.servers]]\nname = '{name}'\ncommand = [{argv}]\n", encoding="utf-8")
    return profile


class TestGuardSameRights:
    def test_mcp_tool_denied_by_whitelist(self, tmp_path: pathlib.Path) -> None:
        """MCP 工具同权过闸（§3.6）：白名单不含 echo → denied 回执且不执行。"""
        guard = GuardConfig(whitelist=("nmap",))
        registry = _registry_with_echo(tmp_path, guard=guard)
        out = registry.dispatch("echo", {"text": "hi"})
        assert out.denied is True
        assert "E_GUARD_DENY" in out.summary
        assert out.exit_code is None


class TestFailureInjection:
    def test_server_crash_mid_call_lands_e_tool_fail(self, tmp_path: pathlib.Path) -> None:
        registry = _registry_with_echo(tmp_path)
        out = registry.dispatch("echo", {"text": "__crash__"})
        assert out.exit_code is None
        assert "E_TOOL_FAIL" in out.summary
        assert out.raw_ref == ""


class TestCliListing:
    def test_secpt_tools_lists_mcp_tool(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("SECPT_PROFILE", _mcp_profile(tmp_path))
        monkeypatch.setenv("SECPT_NMAP_BIN", str(tmp_path / "no-such-nmap"))
        rc = main_mod.main(["tools"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "echo  kind=mcp" in out
        assert "hash=" in out
