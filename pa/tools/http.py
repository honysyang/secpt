"""HTTP 请求工具（BUILD_SPEC §10.2）：httpx，禁用自动跨域重定向（防范围逃逸）。

范围盲（S5 约束 B）：url 是否越界由 gateway 判一次，工具不判。
网络错误不抛异常，落进 ProcResult 的 stderr，由上层决定怎么记。
"""

from __future__ import annotations

import hashlib

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
    # 结构化观测：状态码之外的关键指纹（安全头/跳转/长度/正文哈希）。
    # stdout 仍是 body，旧调用方零变化；worker 通过 clue.meta 看到这些。
    resp_headers = {str(k).lower(): str(v) for k, v in r.headers.items()}
    meta = {
        "status": r.status_code,
        "headers": resp_headers,
        "content_type": resp_headers.get("content-type", ""),
        "location": resp_headers.get("location", ""),
        "content_length": len(r.content),
        "body_sha256": hashlib.sha256(r.content).hexdigest()[:16],
    }
    return ProcResult(
        r.status_code,
        out,
        b"",
        truncated=truncated,
        original_bytes=original,
        meta=meta,
    )
