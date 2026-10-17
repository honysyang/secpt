"""流量武器化：基于录制库的确定性回放与单参数变异。

把 traffic 层从"取证录制"升级为"攻击面复用"：
- list_exchanges：按 host 过滤列出已录请求（武器化目标的候选）
- replay：重建某条录制的请求，应用覆盖（method/url/header/body）后发送
- fuzz_param：以录制请求为基线，对 query/form 单参数逐 payload 替换，
  返回每发结果（状态码 + 长度 + 命中标记）——确定性差分，不靠模型逐发想象

安全边界：发送目标过 scope 校验（loopback 豁免与 engine 一致）；
TLS 默认校验开启（红线）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from secpt.traffic.store import TrafficStore, default_traffic_dir


def _store(job_id: str) -> TrafficStore:
    return TrafficStore(default_traffic_dir() / f"{job_id}.sqlite")


def list_exchanges(job_id: str, host_filter: str = "", limit: int = 20) -> list[dict]:
    """列出一个 job 已录的交换（id/method/url/status/req_body 预览）。"""
    db = default_traffic_dir() / f"{job_id}.sqlite"
    if not Path(db).is_file():
        return []
    store = _store(job_id)
    try:
        rows = (
            store.search(host_filter, limit=limit)
            if host_filter
            else [
                store.get(i)
                for i in range(
                    max(1, (store.stats()["exchanges"] or 0) - limit + 1),
                    store.stats()["exchanges"] + 1,
                )
            ]
        )
        out = []
        for r in rows:
            if not r:
                continue
            body = r.get("req_body") or b""
            out.append(
                {
                    "id": r["id"],
                    "method": r.get("method") or "",
                    "url": str(r.get("url") or "")[:200],
                    "status": r.get("status") or 0,
                    "req_body": body[:120].decode(errors="replace")
                    if isinstance(body, bytes)
                    else str(body)[:120],
                }
            )
        return out
    finally:
        store.close()


def _row_request(row: dict) -> tuple[str, str, dict, bytes]:
    method = str(row.get("method") or "GET")
    url = str(row.get("url") or "")
    headers = {}
    rh = row.get("req_headers")
    if rh:
        try:
            headers = json.loads(rh)
        except (json.JSONDecodeError, TypeError):
            headers = {}
    body = row.get("req_body") or b""
    if isinstance(body, str):
        body = body.encode()
    return method, url, headers, body


def _scope_ok(host: str) -> str | None:
    import os

    if os.environ.get("SECPT_SCOPE", "1") == "0":
        return None
    try:
        from secpt.governance.scope import build_scope_validator
        from secpt.runtime.engine import is_loopback_target

        scope = build_scope_validator(None)
        if scope is None or is_loopback_target(host):
            return None
        scope.validate(host)
    except Exception as e:
        if getattr(e, "scope_denied", False):
            return f"目标 {host} 不在授权范围（scope）"
        return None
    return None


def _apply_overrides(
    method: str, url: str, headers: dict, body: bytes, ov: dict
) -> tuple[str, str, dict, bytes]:
    if ov.get("method"):
        method = str(ov["method"]).upper()
    if ov.get("url"):
        url = str(ov["url"])
    if ov.get("headers"):
        hov = ov["headers"]
        if isinstance(hov, dict):
            headers.update(hov)
        else:
            with contextlib.suppress(json.JSONDecodeError):
                headers.update(json.loads(str(hov)))
    if "body" in ov and ov["body"] is not None:
        body = str(ov["body"]).encode()
    return method, url, headers, body


def _send(method: str, url: str, headers: dict, body: bytes, timeout_s: int) -> dict:
    import httpx

    host = urlsplit(url).netloc
    why = _scope_ok(host)
    if why:
        return {"error": why}
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            r = client.request(method, url, headers=headers, content=body or None)
        return {
            "status": r.status_code,
            "length": len(r.content),
            "body_head": r.text[:200],
            "headers": {k: v for k, v in list(r.headers.items())[:20]},
        }
    except Exception as e:
        return {"error": f"请求失败: {e}"}


async def replay(
    job_id: str,
    exchange_id: int,
    overrides: dict | None = None,
    *,
    timeout_s: int = 15,
) -> dict:
    """重建录制请求并发送（overrides 支持 method/url/headers/body）。"""
    store = _store(job_id)
    try:
        row = store.get(int(exchange_id))
    finally:
        store.close()
    if row is None:
        return {"error": f"交换 #{exchange_id} 不存在"}
    method, url, headers, body = _row_request(row)
    method, url, headers, body = _apply_overrides(method, url, headers, body, overrides or {})
    return await asyncio.to_thread(_send, method, url, headers, body, timeout_s)


def _vary_param(url: str, body: bytes, headers: dict, param: str, value: str) -> tuple[str, bytes]:
    """单参数替换：query 优先，其次 urlencoded form body。"""
    parts = urlsplit(url)
    qs = parse_qsl(parts.query, keep_blank_values=True)
    if any(k == param for k, _ in qs):
        new_qs = urlencode([(k, value if k == param else v) for k, v in qs])
        return urlunsplit(parts._replace(query=new_qs)), body
    ctype = str(headers.get("Content-Type") or headers.get("content-type") or "")
    if "application/x-www-form-urlencoded" in ctype:
        form = parse_qsl(body.decode(errors="replace"), keep_blank_values=True)
        if any(k == param for k, _ in form):
            return url, urlencode([(k, value if k == param else v) for k, v in form]).encode()
    # 参数没找到：追加到 query（仍返回可发请求，结果里标注）
    sep = "&" if parts.query else ""
    return urlunsplit(parts._replace(query=f"{parts.query}{sep}{param}={value}")), body


async def fuzz_param(
    job_id: str,
    exchange_id: int,
    param: str,
    payloads: list[str],
    *,
    marker: str = "",
    timeout_s: int = 10,
) -> dict:
    """单参数逐 payload 差分。返回基线 + 每发结果（状态/长度/命中）。"""
    store = _store(job_id)
    try:
        row = store.get(int(exchange_id))
    finally:
        store.close()
    if row is None:
        return {"error": f"交换 #{exchange_id} 不存在"}
    if not payloads:
        return {"error": "payloads 为空"}
    method, url, headers, body = _row_request(row)
    baseline = await asyncio.to_thread(_send, method, url, headers, body, timeout_s)
    results = []
    for p in payloads[:50]:  # 上限 50 发/次，防烧预算
        u2, b2 = _vary_param(url, body, headers, param, str(p))
        r = await asyncio.to_thread(_send, method, u2, headers, b2, timeout_s)
        hit = bool(marker) and marker in str(r.get("body_head") or "")
        results.append({"payload": str(p)[:100], **r, "marker_hit": hit})
    return {"baseline": baseline, "param": param, "results": results}
