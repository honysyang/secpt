"""Registry 装配测试。"""

from __future__ import annotations

from secpt.config.models import ToolsConfig
from secpt.core.tools.registry import build_registry


class TestRegistryAssembly:
    """测试工具装配逻辑。"""

    def test_default_tool_names_includes_all(self) -> None:
        """默认 tool_names 应包含所有关键工具。"""
        reg = build_registry()
        names = reg.names()
        for expected in ["saml", "crawler", "http_analyze", "flag_extract", "read_file"]:
            assert expected in names, f"missing tool: {expected}"

    def test_command_whitelist_does_not_remove_python_tools(self) -> None:
        """收紧 command_whitelist 不应导致纯 Python 工具消失。"""
        reg = build_registry(
            whitelist=[],  # 无命令行工具
            tool_names=["crawler", "saml", "flag_extract", "read_file"],
        )
        names = reg.names()
        # 纯 Python 工具不受 command_whitelist 限制
        assert "crawler" in names
        assert "saml" in names
        assert "flag_extract" in names
        assert "read_file" in names

    def test_unknown_tool_names_skipped(self) -> None:
        """未知工具名应静默跳过，不报错。"""
        reg = build_registry(
            whitelist=[],
            tool_names=["crawler", "nonexistent_tool", "also_fake"],
        )
        names = reg.names()
        assert "crawler" in names
        assert "nonexistent_tool" not in names
        assert len(names) == 1

    def test_tools_config_backward_compat(self) -> None:
        """ToolsConfig.whitelist 是 command_whitelist 的别名。"""
        cfg = ToolsConfig.model_validate({"whitelist": ["nmap", "curl"]})
        assert cfg.command_whitelist == ["nmap", "curl"]
        assert cfg.whitelist == ["nmap", "curl"]

    def test_tools_config_tool_names_default(self) -> None:
        """ToolsConfig.tool_names 有合理默认值。"""
        cfg = ToolsConfig()
        assert "saml" in cfg.tool_names
        assert "crawler" in cfg.tool_names
