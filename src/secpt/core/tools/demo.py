"""演示工具：echo。

通过 :func:`secpt.runtime.subprocess.run_whitelisted` 调用系统 ``echo``，
把异常转成 ``"Error: ..."`` 字符串，确保 runner 不炸。
"""

from __future__ import annotations

from secpt.core.tools.base import Tool
from secpt.runtime.subprocess import run_whitelisted


class EchoTool(Tool):
    """echo 参数并返回 stdout。"""

    name = "echo"
    description = "把参数原样回显（用于协议冒烟测试）"

    async def run(self, args: str) -> str:
        try:
            result = await run_whitelisted("echo", [args.strip()])
        except Exception as exc:
            return f"Error: {exc}"
        return result.stdout.strip()
