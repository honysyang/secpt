"""TCP 探活：connect_ex，open → exit 0 / closed → exit 1。范围盲（S5 约束 B）。"""

from __future__ import annotations

import socket

from pa import envs
from pa.tools.base import ToolContext, ProcResult


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    host = str(args.get("target", ""))
    port = int(args.get("port", 0))
    # probe 是秒级动作：gateway 传来的 ctx.timeout（默认 120s）是给 nmap/
    # http 这类重活的预算，raw TCP 单连接取 min(ctx.timeout, probe 天花板)，
    # closed/filtered 端口不再把一个 ReAct 步耗成两分钟白等（质量项②）。
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(min(ctx.timeout, envs.probe_timeout()))
    try:
        rc = s.connect_ex((host, port))
    except OSError as e:
        return ProcResult(1, b"", f"{type(e).__name__}: {e}".encode())
    finally:
        s.close()
    if rc == 0:
        return ProcResult(0, b"open\n", b"")
    return ProcResult(1, b"closed\n", b"")
