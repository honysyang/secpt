"""真实侦察工具：nmap / curl / dig。

约束：
- 仅调用 :func:`secpt.runtime.subprocess.run_whitelisted` 白名单内的命令
- 输出截断到 MAX_OUTPUT_CHARS，防止撑爆上下文
- 异常统一转 "Error: ..." 字符串，不抛
- 逐工具超时从 settings.tools.tool_timeouts 读取，不用默认值
"""

from __future__ import annotations

import re

from secpt.core.tools.base import Tool
from secpt.runtime.subprocess import run_whitelisted

MAX_OUTPUT_CHARS = 4000

# 全端口扫描 pattern：-p 1-65535 / -p 1-10000 / -p-
_FULL_PORT_RE = re.compile(r"(-p[\s=]*1-\d+|-p-)(?=\s|$)")


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {len(text)} chars]"


def _get_tool_timeout(tool_name: str, default: float = 30.0) -> float:
    """从 settings.tools.tool_timeouts 读取逐工具超时。"""
    try:
        from secpt.config.settings import get_settings
        settings = get_settings()
        return settings.tools.tool_timeouts.get(tool_name, default)
    except Exception:
        return default


def _rewrite_nmap_args(args: str) -> tuple[str, str | None]:
    """改写 nmap 全端口扫描参数为 --top-ports 200。

    返回 (改写后参数, 改写说明 or None)。
    """
    if _FULL_PORT_RE.search(args):
        rewritten = _FULL_PORT_RE.sub("--top-ports 200", args)
        note = f"[nmap 参数改写] 全端口扫描已自动改为 --top-ports 200（原参数: {args.strip()[:60]}）"
        return rewritten, note
    return args, None


class NmapTool(Tool):
    """nmap 端口扫描。

    参数格式：``-p 1-1000 -T4 <target>``（透传给 nmap）
    全端口扫描（-p 1-65535 / -p-）自动改写为 --top-ports 200。
    """

    name = "nmap"
    description = "nmap 端口扫描：参数如 '-p 1-1000 -T4 127.0.0.1'（全端口自动改为 --top-ports 200）"

    async def run(self, args: str) -> str:
        try:
            argv_str, note = _rewrite_nmap_args(args)
            argv = argv_str.split()
            timeout = _get_tool_timeout("nmap", 45.0)
            result = await run_whitelisted("nmap", argv, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0 and not output.strip():
            return f"Error: nmap exit {result.returncode}"
        if note:
            output = note + "\n" + output
        return _truncate(output.strip())


class CurlTool(Tool):
    """curl HTTP 请求。

    参数格式：``-sS -o /dev/null -w '%{http_code}' <url>``（透传给 curl）
    默认 30s 超时。
    """

    name = "curl"
    description = "curl HTTP 请求：参数如 '-sS http://127.0.0.1:8080/'"

    async def run(self, args: str) -> str:
        try:
            argv = args.split()
            timeout = _get_tool_timeout("curl", 15.0)
            result = await run_whitelisted("curl", argv, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0 and not output.strip():
            return f"Error: curl exit {result.returncode}"
        return _truncate(output.strip())


class DigTool(Tool):
    """dig DNS 查询。

    参数格式：``<domain> [A|AAAA|MX|TXT|NS]``
    """

    name = "dig"
    description = "dig DNS 查询：参数如 'example.com A'"

    async def run(self, args: str) -> str:
        try:
            argv = args.split()
            timeout = _get_tool_timeout("dig", 10.0)
            result = await run_whitelisted("dig", argv, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0 and not output.strip():
            return f"Error: dig exit {result.returncode}"
        return _truncate(output.strip())
