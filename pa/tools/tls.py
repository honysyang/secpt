"""TLS 握手与证书采集：连接目标端口，输出协议版本、密码套件、证书
摘要与链校验结果。范围盲（S5），超时经 ToolContext。

exit 0 = 握手成功（对面能谈 TLS；verified 字段单独表达链信任 + 主机名
校验结果，证书过期/自签不等于握手失败）；exit 1 = 连接或握手失败
（对端非 TLS、拒绝连接等）。依赖仅标准库 ssl/socket。"""

from __future__ import annotations

import os
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path

from pa.tools.base import ToolContext, ProcResult

_DEFAULT_PORT = 443


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    host = str(args.get("target", "")).strip()
    if not host:
        return ProcResult(1, b"", b"target required")
    port = int(args.get("port") or _DEFAULT_PORT)

    # 1) 不校验链的握手：只要对面能谈 TLS 就算数，证书解析出来供评估
    nv = ssl.create_default_context()
    nv.check_hostname = False
    nv.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=ctx.timeout) as sock:
            with nv.wrap_socket(sock, server_hostname=host) as ss:
                proto = ss.version() or "unknown"
                cipher = (ss.cipher() or ("unknown",))[0]
                der = ss.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as e:
        return ProcResult(1, b"", f"{type(e).__name__}: {e}".encode())

    fields = _decode_cert(der, ctx.workspace)
    verified, verify_err = _verify(host, port, ctx.timeout)

    lines = [
        f"handshake={proto}",
        f"cipher={cipher}",
        f"verified={'true' if verified else 'false'}",
    ]
    if verify_err:
        lines.append(f"verify_error={verify_err}")
    for key in ("subject", "issuer", "notBefore", "notAfter"):
        if fields.get(key):
            lines.append(f"cert_{key}={fields[key]}")
    days = _days_left(str(fields.get("notAfter") or ""))
    if days is not None:
        lines.append(f"days_left={days}")
    san = fields.get("subjectAltName") or []
    if san:
        lines.append(f"san={len(san)}")
    return ProcResult(0, ("\n".join(lines) + "\n").encode("utf-8"), b"")


def _decode_cert(der: bytes, workspace: Path) -> dict:
    """DER → 字段 dict。stdlib 无公开 DER 解析接口，用 _test_decode_cert
    （读 PEM 文件）解码；不可用或解析失败时退回空 dict（握手信息仍在）。"""
    decode = getattr(ssl._ssl, "_test_decode_cert", None)  # noqa: SLF001
    if decode is None or not der:
        return {}
    pem = ssl.DER_cert_to_PEM_cert(der)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / f"tls-cert-{abs(hash(pem)) & 0xFFFFFFFF:x}.pem"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, pem.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        raw = decode(str(path))
    except (OSError, ValueError, IndexError):
        return {}
    out: dict = {}
    for key in ("notBefore", "notAfter", "serialNumber"):
        if raw.get(key):
            out[key] = str(raw[key])
    for key in ("subject", "issuer"):
        value = raw.get(key)
        if value:
            out[key] = ", ".join(f"{k}={v}" for rdn in value for k, v in rdn)
    if raw.get("subjectAltName"):
        out["subjectAltName"] = list(raw["subjectAltName"])
    return out


def _verify(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """默认上下文二次连接：链信任 + 主机名校验。失败原因可观测
    （证书过期/自签/主机名不符都属「有结论」的评估数据，不是工具错误）。"""
    vctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with vctx.wrap_socket(sock, server_hostname=host):
                return True, ""
    except ssl.SSLCertVerificationError as e:
        return False, str(e.verify_message or e)
    except (OSError, ssl.SSLError) as e:
        return False, f"{type(e).__name__}: {e}"


def _days_left(not_after: str) -> int | None:
    """'Dec  7 12:00:00 2026 GMT' → 距今天数（负 = 已过期）。"""
    if not not_after:
        return None
    try:
        dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y GMT")
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).days
