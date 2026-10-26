"""DNS 解析工具（stdlib）：A/AAAA 地址清单，作为侦察基线。

为什么单独做工具而不是让 shell 拼 dig/nslookup：
- shell 白名单不含 nslookup/getent/host，worker 反复试错（真跑 t-0001）；
- dig +short 的输出没有 key=value，extract_clues 无法落线索；
- DNS 需要独立的证据类型 dns.result，才能被判据/验收直接使用。

范围盲（S5）：target 是否在范围内由 gateway 判定。解析失败返回非零
exit + stderr，由上层记 dns.error，不抛异常。
"""

from __future__ import annotations

import socket

from pa.tools.base import ToolContext, ProcResult


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    host = str(args.get("target") or "").strip()
    if not host:
        return ProcResult(1, b"", b"target required")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return ProcResult(1, b"", f"{type(e).__name__}: {e}".encode())
    ips = sorted({str(info[4][0]) for info in infos if info[4]})
    if not ips:
        return ProcResult(1, b"", b"no addresses")
    lines = [f"ip={ip}" for ip in ips]
    return ProcResult(
        0,
        ("\n".join(lines) + "\n").encode("utf-8"),
        b"",
        meta={"host": host, "ips": ips},
    )
