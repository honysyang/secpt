"""受限 shell：命令前缀白名单（BUILD_SPEC §10.1）。

v0 白名单（"工具只能减不能加"的最小实现）：nmap / curl / dig / whoami / id / ls。
未命中白名单返回 exit_code=126（blocked），进程不执行。范围盲（S5 约束 B）：
args 里的 target 只供 gateway 做范围校验，工具自身不判定。
"""
from __future__ import annotations

import shlex

from pa.infra.executor import run
from pa.tools.base import ToolContext, ProcResult

ALLOWED_PREFIXES = ("nmap", "curl", "dig", "whoami", "id", "ls")


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    try:
        parts = shlex.split(args.get("cmd", ""))
    except ValueError as e:
        return ProcResult(126, b"", f"blocked:bad_cmd:{e}".encode())
    if not parts or parts[0] not in ALLOWED_PREFIXES:
        exe = parts[0] if parts else ""
        return ProcResult(126, b"", f"blocked:command_not_allowed:{exe}".encode())
    return run(parts, timeout=ctx.timeout, workdir=ctx.workspace)
