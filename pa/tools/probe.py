"""TCP 探活：connect_ex，open → exit 0 / closed → exit 1。范围盲（S5 约束 B）。"""
from __future__ import annotations

import socket

from pa.tools.base import ToolContext, ProcResult


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    host = str(args.get("target", ""))
    port = int(args.get("port", 0))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(ctx.timeout)
    try:
        rc = s.connect_ex((host, port))
    except OSError as e:
        return ProcResult(1, b"", f"{type(e).__name__}: {e}".encode())
    finally:
        s.close()
    if rc == 0:
        return ProcResult(0, b"open\n", b"")
    return ProcResult(1, b"closed\n", b"")
