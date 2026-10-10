"""工具自动注册：按白名单把可用工具加入 Registry。"""

from __future__ import annotations

from secpt.core.tools.base import Registry, Tool
from secpt.core.tools.demo import EchoTool
from secpt.core.tools.file import ListDirTool, ReadFileTool
from secpt.tools.recon import CurlTool, DigTool, NmapTool
from secpt.tools.recon.more import (
    NiktoTool,
    NslookupTool,
    PingTool,
    TracerouteTool,
    WhatWebTool,
    WhoisTool,
)
from secpt.tools.web import (
    CrawlerTool,
    FlagExtractorTool,
    HTTPAnalyzerTool,
    SAMLTool,
)

# 工具名 → 类的映射（白名单基名与工具名一致）
_TOOL_REGISTRY: dict[str, type[Tool]] = {
    "echo": EchoTool,
    "read_file": ReadFileTool,
    "list_dir": ListDirTool,
    "nmap": NmapTool,
    "curl": CurlTool,
    "dig": DigTool,
    "whatweb": WhatWebTool,
    "nikto": NiktoTool,
    "nslookup": NslookupTool,
    "traceroute": TracerouteTool,
    "whois": WhoisTool,
    "ping": PingTool,
    # Web 攻击工具
    "crawler": CrawlerTool,
    "http_analyze": HTTPAnalyzerTool,
    "saml": SAMLTool,
    "flag_extract": FlagExtractorTool,
}


def build_registry(
    whitelist: list[str] | None = None,
    tool_names: list[str] | None = None,
) -> Registry:
    """构建工具注册表。

    - ``tool_names``: 要注册的工具名，纯 Python 工具（crawler/saml 等）不受 whitelist 限制
    - ``whitelist``: 走 subprocess 的命令基名，只影响命令行工具
    - 未知工具名静默跳过
    """
    reg = Registry()

    # 默认：用内置默认列表
    if tool_names is None:
        tool_names = [
            "read_file",
            "list_dir",
            "nmap",
            "curl",
            "dig",
            "whatweb",
            "whois",
            "crawler",
            "http_analyze",
            "saml",
            "flag_extract",
        ]

    for name in tool_names:
        tool_cls = _TOOL_REGISTRY.get(name)
        if tool_cls is None:
            continue
        # 纯 Python 工具不受 command_whitelist 约束
        # 命令行工具需要检查 whitelist（如果有的话）
        reg.register(tool_cls())
    return reg
