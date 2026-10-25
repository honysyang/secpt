"""HTTP 请求工具（BUILD_SPEC §10.2）：httpx，禁用自动跨域重定向（防范围逃逸）。

范围盲（S5 约束 B）：url 是否越界由 gateway 判一次，工具不判。
网络错误不抛异常，落进 ProcResult 的 stderr，由上层决定怎么记。
"""
from __future__ import annotations

import httpx

from pa.tools.base import ToolContext, ProcResult, cap_stdout


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    method = str(args.get("method", "GET")).upper()
    url = str(args.get("url", ""))
    headers = args.get("headers") or {}
    body = args.get("body")
    try:
        with httpx.Client(timeout=ctx.timeout, follow_redirects=False) as c:
            r = c.request(method, url, headers=headers, content=body)
    except httpx.HTTPError as e:
        return ProcResult(1, b"", f"{type(e).__name__}: {e}".encode())
    out, truncated, original = cap_stdout(r.content)
    return ProcResult(r.status_code, out, b"",
                      truncated=truncated, original_bytes=original)
