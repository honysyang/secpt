"""网络请求内置工具。

安全设计：
- 仅允许 http/https 协议（拒绝 file://、ftp:// 等绕过）
- 响应体有长度上限，防止超大响应耗尽上下文
"""

from __future__ import annotations

import asyncio
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from harness.tools.decorator import tool

_DEFAULT_MAX_LENGTH = 10_000
_ALLOWED_SCHEMES = {"http", "https"}


@tool
async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: float = 30.0,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> str:
    """发起 HTTP 请求并返回状态码与响应体。

    Args:
        url: 完整 URL（仅支持 http/https）
        method: HTTP 方法（GET/POST/PUT/DELETE 等）
        headers: 请求头
        body: 请求体（文本）
        timeout: 超时秒数
        max_length: 响应体最大返回长度（超出截断）
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"错误: 仅支持 {sorted(_ALLOWED_SCHEMES)} 协议，收到 {scheme or '(空)'}"

    data = body.encode("utf-8") if body is not None else None
    request = Request(url, data=data, headers=headers or {}, method=method.upper())  # noqa: S310 -- scheme 已白名单

    def _sync() -> tuple[int, str, str]:
        with urlopen(request, timeout=timeout) as resp:  # noqa: S310 -- 同上
            raw = resp.read(max_length + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.headers.get("Content-Type", ""), raw[:max_length].decode(charset, errors="replace")

    try:
        status, content_type, text = await asyncio.to_thread(_sync)
    except HTTPError as exc:
        return f"错误: HTTP {exc.code} {exc.reason}"
    except URLError as exc:
        return f"错误: 请求失败 {exc.reason}"
    except TimeoutError:
        return f"错误: 请求超时（>{timeout}s）"

    header = f"status: {status}\ncontent-type: {content_type}"
    if len(text) >= max_length:
        text += f"\n... [已截断，max_length={max_length}]"
    return f"{header}\n\n{text}"
