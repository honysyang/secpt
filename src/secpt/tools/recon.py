"""真实侦察工具：nmap / curl / dig。

约束：
- 仅调用 :func:`secpt.runtime.subprocess.run_whitelisted` 白名单内的命令
- 输出截断到 MAX_OUTPUT_CHARS，防止撑爆上下文
- 异常统一转 "Error: ..." 字符串，不抛
"""

from __future__ import annotations

from secpt.core.tools.base import Tool
from secpt.runtime.subprocess import run_whitelisted

MAX_OUTPUT_CHARS = 4000


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {len(text)} chars]"


class NmapTool(Tool):
    """nmap 端口扫描。

    参数格式：``-p 1-1000 -T4 <target>``（透传给 nmap）
    """

    name = "nmap"
    description = "nmap 端口扫描：参数如 '-p 1-1000 -T4 127.0.0.1'"

    async def run(self, args: str) -> str:
        try:
            argv = args.split()
            result = await run_whitelisted("nmap", argv, timeout=120.0)
        except Exception as exc:
            return f"Error: {exc}"
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0 and not output.strip():
            return f"Error: nmap exit {result.returncode}"
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
            result = await run_whitelisted("curl", argv, timeout=30.0)
        except Exception as exc:
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
            result = await run_whitelisted("dig", argv, timeout=15.0)
        except Exception as exc:
            return f"Error: {exc}"
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        if result.returncode != 0 and not output.strip():
            return f"Error: dig exit {result.returncode}"
        return _truncate(output.strip())
