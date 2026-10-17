"""http_request 插件：结构化 HTTP 原语 + raw 模式。

动机：HTTP 利用走 bash curl 受 argv 引号转义拖累（请求走私、畸形 header、
分块编码的手工构造很痛苦）。本工具 method/headers/body 分字段传参；
raw 模式按字节原样发送（走私/畸形请求研究用，TLS 仍走系统默认校验——
红线：绝不像 -k 那样关校验）。

治理：目标 host 过 scope 校验（loopback 豁免与 engine 一致）。
profile 装配：tools: [http_request] 声明即装配。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from urllib.parse import urlsplit

SCHEMA = {
    "type": "object",
    "description": (
        "发 HTTP 请求并返回状态/响应头/响应体（截断）。普通模式结构化传参；"
        "raw 模式把 body 字段当作完整请求字节原样发送（需自行含 Host 头）。"
    ),
    "properties": {
        "method": {"type": "string", "description": "HTTP 方法（默认 GET）"},
        "url": {"type": "string", "description": "完整 URL（普通模式必填）"},
        "headers": {
            "type": "string",
            "description": '请求头 JSON 对象文本，如 {"X-Test": "1"}',
        },
        "body": {"type": "string", "description": "请求体（普通模式）；raw 模式=完整请求字节"},
        "raw": {"type": "boolean", "description": "true 时 body 原样发送（走私/畸形请求）"},
        "host": {"type": "string", "description": "raw 模式必填：目标 host:port"},
        "tls": {"type": "boolean", "description": "raw 模式：是否 TLS（默认按 443 判断）"},
        "timeout_s": {"type": "integer", "description": "超时（默认 15s）"},
        "max_bytes": {"type": "integer", "description": "响应体截取上限（默认 8000）"},
    },
    "required": [],
}

_OUT_CAP = 8000


def _scope_ok(host: str) -> str | None:
    if os.environ.get("SECPT_SCOPE", "1") == "0":
        return None
    try:
        from secpt.governance.scope import build_scope_validator
        from secpt.runtime.engine import is_loopback_target

        scope = build_scope_validator(None)
        if scope is None:
            return None
        if is_loopback_target(host):
            return None
        scope.validate(host)
    except Exception as e:
        if getattr(e, "scope_denied", False):
            return f"目标 {host} 不在授权范围（scope）"
        return None  # 校验器异常 → fail-open（engine 同口径）
    return None


def _fmt(method: str, url: str, status: int, headers: dict, body: str, limit: int) -> str:
    h = "\n".join(f"{k}: {v}" for k, v in list(headers.items())[:30])
    return (
        f"{method} {url}\n< {status}\n{h}\n\n{body[:limit]}"
        if h
        else f"{method} {url}\n< {status}\n\n{body[:limit]}"
    )


def _normal(args: dict) -> str:
    import httpx

    url = str(args.get("url") or "")
    if not url:
        return "Error: 普通模式需要 url 参数"
    method = str(args.get("method") or "GET").upper()
    headers = {}
    if args.get("headers"):
        try:
            headers = json.loads(str(args["headers"]))
        except json.JSONDecodeError as e:
            return f"Error: headers 不是合法 JSON: {e}"
    body = args.get("body")
    timeout_s = max(1, min(60, int(args.get("timeout_s") or 15)))
    limit = max(256, min(64_000, int(args.get("max_bytes") or _OUT_CAP)))

    host = urlsplit(url).netloc
    why = _scope_ok(host)
    if why:
        return f"Error: {why}"
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            r = client.request(method, url, headers=headers, content=body)
        return _fmt(method, url, r.status_code, dict(r.headers), r.text, limit)
    except Exception as e:
        return f"Error: 请求失败: {e}"


def _raw(args: dict) -> str:
    host = str(args.get("host") or "")
    payload = str(args.get("body") or "")
    if not host or not payload:
        return "Error: raw 模式需要 host 与 body（完整请求字节）"
    timeout_s = max(1, min(60, int(args.get("timeout_s") or 15)))
    limit = max(256, min(64_000, int(args.get("max_bytes") or _OUT_CAP)))
    why = _scope_ok(host)
    if why:
        return f"Error: {why}"

    host_only, _, port_s = host.partition(":")
    port = int(port_s) if port_s else 443
    tls = bool(args.get("tls")) if "tls" in args else port == 443
    try:
        sock = socket.create_connection((host_only, port), timeout=timeout_s)
        if tls:
            import ssl

            # 系统默认校验开启（红线：绝不关 TLS 校验）
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host_only)
        sock.sendall(payload.encode())
        chunks = []
        sock.settimeout(timeout_s)
        try:
            while sum(len(c) for c in chunks) < limit:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        finally:
            sock.close()
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace") if head else "(无响应)"
        return f"> raw → {host}\n< {status}\n\n{body[:limit].decode(errors='replace')}"
    except Exception as e:
        return f"Error: raw 发送失败: {e}"


async def _http_request(args: dict) -> str:
    args = args or {}
    if args.get("raw"):
        return await asyncio.to_thread(_raw, args)
    return await asyncio.to_thread(_normal, args)


def register() -> dict:
    return {"http_request": (SCHEMA, _http_request)}
