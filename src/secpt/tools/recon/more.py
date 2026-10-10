"""更多真实侦察工具。

每个工具：
- 走 :func:`secpt.runtime.subprocess.run_whitelisted`
- 输出截断到 MAX_OUTPUT_CHARS
- 异常统一转 "Error: ..." 字符串
"""

from __future__ import annotations

from secpt.core.tools.base import Tool
from secpt.runtime.subprocess import run_whitelisted

MAX_OUTPUT_CHARS = 4000


def _get_tool_timeout(tool_name: str, default: float = 30.0) -> float:
    """从 settings.tools.tool_timeouts 读取逐工具超时。"""
    try:
        from secpt.config.settings import get_settings

        settings = get_settings()
        return settings.tools.tool_timeouts.get(tool_name, default)
    except Exception:
        return default


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {len(text)} chars]"


def _run(cmd: str, argv: list[str], timeout: float = 30.0) -> str:
    """执行命令并规整输出。"""
    import asyncio

    return asyncio.get_event_loop().run_until_complete(_run_async(cmd, argv, timeout))


async def _run_async(cmd: str, argv: list[str], timeout: float) -> str:
    try:
        result = await run_whitelisted(cmd, argv, timeout=timeout)
    except Exception as exc:
        return f"Error: {exc}"
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    if result.returncode != 0 and not output.strip():
        return f"Error: {cmd} exit {result.returncode}"
    return _truncate(output.strip())


class WhatWebTool(Tool):
    """Web 指纹识别。

    参数格式：``<url>``（如 http://127.0.0.1:8080/）
    """

    name = "whatweb"
    description = "Web 指纹识别：参数如 'http://127.0.0.1:8080/'"

    async def run(self, args: str) -> str:
        return await _run_async("whatweb", args.split(), timeout=_get_tool_timeout("whatweb", 20.0))


class NiktoTool(Tool):
    """Web 漏洞扫描。

    参数格式：``-h <host> -p <port>``
    """

    name = "nikto"
    description = "Web 漏洞扫描：参数如 '-h 127.0.0.1 -p 8080'"

    async def run(self, args: str) -> str:
        return await _run_async("nikto", args.split(), timeout=120.0)


class NslookupTool(Tool):
    """DNS 查询（nslookup）。

    参数格式：``<domain> [server]``
    """

    name = "nslookup"
    description = "DNS 查询：参数如 'example.com'"

    async def run(self, args: str) -> str:
        return await _run_async("nslookup", args.split(), timeout=15.0)


class TracerouteTool(Tool):
    """路由追踪。

    参数格式：``<host>``
    """

    name = "traceroute"
    description = "路由追踪：参数如 '10.0.0.1'"

    async def run(self, args: str) -> str:
        return await _run_async("traceroute", args.split(), timeout=30.0)


class WhoisTool(Tool):
    """WHOIS 查询。

    参数格式：``<domain>``
    """

    name = "whois"
    description = "WHOIS 查询：参数如 'example.com'"

    async def run(self, args: str) -> str:
        return await _run_async("whois", args.split(), timeout=_get_tool_timeout("whois", 10.0))


class PingTool(Tool):
    """主机存活检测。

    参数格式：``-c 4 <host>``
    """

    name = "ping"
    description = "主机存活：参数如 '-c 4 127.0.0.1'"

    async def run(self, args: str) -> str:
        return await _run_async("ping", args.split(), timeout=15.0)
