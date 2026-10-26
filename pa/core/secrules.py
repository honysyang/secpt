"""确定性安全规则引擎（对标 CAI 的技巧库 / PentAGI 的审计员：规则先行，
LLM 只补规则覆盖不到的结论）。

设计立场：
- **规则产出 finding，不直接下结论**：与 sensitive_finding 同一管道——
  规则命中即提案 finding.proposed，交 JEV 裁决（suspected/confirmed），
  证据链与反证条件齐备后才进报告正文。误报的责任链不因规则化而消失；
- **每条规则自带 falsify_if**：反证条件是判定的一部分，不是装饰；
- **幂等可重放**：同一输入恒定输出，finding 身份（claim/reproduce/
  asset_id）跨重放稳定，重复提案靠稳定 id 去重。

三族规则：
1. header_finding：缺失安全响应头（HSTS/CSP/XFO/XCTO/RP/PP）；
2. tls_findings：过时协议、弱套件、链校验失败、证书临期；
3. catchall_finding：多路径同构 200（SPA 回退），敏感路径命中不可信——
   真跑 lake.aimohu.com 的 /.git/config 误报的确定性解法。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pa.models import FindingProposal

# 缺失即提案的响应头（display 名 → meta.headers 的 key，全小写）
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Strict-Transport-Security", "strict-transport-security"),
    ("Content-Security-Policy", "content-security-policy"),
    ("X-Frame-Options", "x-frame-options"),
    ("X-Content-Type-Options", "x-content-type-options"),
    ("Referrer-Policy", "referrer-policy"),
    ("Permissions-Policy", "permissions-policy"),
)
# 这两个缺失直接 low（其余 info）：可被浏览器强制的现代基线
_LOW_HEADERS = {"Strict-Transport-Security", "Content-Security-Policy"}

# 弱密码套件特征（cipher 字段包含即命中）
_WEAK_CIPHER_TOKENS = ("RC4", "3DES", "DES-", "NULL", "EXP", "SEED", "IDEA")
# 过时协议（handshake 字段）
_DEPRECATED_PROTOCOLS = {"TLSv1", "TLSv1.0", "TLSv1.1", "SSLv3"}
# 证书临期阈值（天）
_CERT_EXPIRY_DAYS = 14


def _origin_of(url: str) -> str:
    u = urlsplit(url)
    netloc = u.netloc or url
    scheme = u.scheme or "http"
    return f"{scheme}://{netloc}"


def header_finding(
    url: str, status: int, headers: dict, evidence_id: str | None
) -> FindingProposal | None:
    """2xx/3xx 响应缺失安全响应头 → 一条合并 finding（按站点聚合）。

    HSTS 只对 https 有意义；其余对任意 scheme 检查。headers 为空字典
    视为未采集（不提案——「没采到」和「确认缺失」是两回事）。"""
    if not (200 <= status < 400) or not headers:
        return None
    u = urlsplit(url)
    missing: list[str] = []
    for display, key in _SECURITY_HEADERS:
        if key == "strict-transport-security" and (u.scheme or "http") != "https":
            continue
        if not str(headers.get(key) or "").strip():
            missing.append(display)
    if not missing:
        return None
    sev = "low" if set(missing) & _LOW_HEADERS else "info"
    origin = _origin_of(url)
    return FindingProposal(
        claim=f"{origin} 缺失安全响应头：{'、'.join(missing)}",
        severity=sev,  # type: ignore[arg-type]
        evidence_refs=[evidence_id] if evidence_id else [],
        reproduce=f"curl -sI {origin}/ 检查响应头",
        falsify_if=[
            "响应由反代/网关注入这些头（工具直连未经过）",
            "这些头在子路径存在而根路径被业务覆盖",
        ],
        asset_id=origin,
    )


def tls_findings(fields: dict, evidence_id: str | None) -> list[FindingProposal]:
    """TLS 握手结果 → 规则 findings。fields 是 tls.cert clue 的 value
    （extract_clues 从 stdout 解析的 k=v 字段）。一条握手可产出多条独立
    finding（协议/套件/链/有效期正交）。"""
    out: list[FindingProposal] = []
    target = str(fields.get("key") or "")
    proto = str(fields.get("handshake") or "")
    cipher = str(fields.get("cipher") or "")
    verified = str(fields.get("verified") or "").lower()
    days_raw = str(fields.get("days_left") or "")

    def _f(claim: str, sev: str, falsify: list[str]) -> FindingProposal:
        return FindingProposal(
            claim=claim,
            severity=sev,  # type: ignore[arg-type]
            evidence_refs=[evidence_id] if evidence_id else [],
            reproduce=f"对 {target or '目标'} 端口执行 tls 握手检查",
            falsify_if=falsify,
        )

    if proto in _DEPRECATED_PROTOCOLS:
        out.append(
            _f(
                f"{target} 协商到过时 TLS 协议 {proto}（建议 TLSv1.2+）",
                "med",
                ["客户端强制最低版本导致，服务端已禁用旧版本",
                 "握手结果来自中间盒而非目标本身"],
            )
        )
    if any(tok in cipher.upper() for tok in _WEAK_CIPHER_TOKENS):
        out.append(
            _f(
                f"{target} 协商到弱密码套件 {cipher}",
                "high",
                ["套件仅在客户端不声明现代套件时退化（需要重测优先级）",
                 "握手结果来自中间盒而非目标本身"],
            )
        )
    if verified == "false":
        out.append(
            _f(
                f"{target} 证书链校验未通过（自签/主机名不符/过期）",
                "low",
                ["校验环境缺 CA 根（离线环境误报）",
                 "目标本就是内网自签（按部署基线评估）"],
            )
        )
    if days_raw.isdigit() and 0 <= int(days_raw) < _CERT_EXPIRY_DAYS:
        out.append(
            _f(
                f"{target} TLS 证书将在 {days_raw} 天内过期",
                "low",
                ["证书已续期而本地解析到旧链",
                 "评估时间晚于续期窗口"],
            )
        )
    return out


_SENSITIVE_IN_URL = re.compile(
    r"(\.git/|\.svn/|\.env([./?]|$)|\.aws/|\.ssh/|\.bak([./?]|$)|admin|backup)",
    re.IGNORECASE,
)


def has_sensitive_path(urls: list[str]) -> bool:
    """URL 集里是否有敏感形态路径（catch-all 判定的触发条件之一）。"""
    for u in urls:
        path = urlsplit(u).path or "/"
        if _SENSITIVE_IN_URL.search(path):
            return True
    return False


def catchall_finding(
    urls: list[str], evidence_id: str | None
) -> FindingProposal:
    """多路径同构 200 → catch-all 观察 finding（info）。

    命中即意味着：这些路径上的「敏感文件可访问」不成立——本 finding
    同时是对 sensitive_finding 误报的系统性纠正（真跑 lake.aimohu.com
    的 /.git/config 200 实例）。claim 不含动态 URL 列表：后续更多路径
    同构时身份不变，靠稳定 id 合并累积。"""
    origin = _origin_of(urls[0]) if urls else ""
    return FindingProposal(
        claim=(
            f"{origin} 对不同路径返回同构响应（catch-all/SPA 回退），"
            "路径级命中不可信"
        ),
        severity="info",
        evidence_refs=[evidence_id] if evidence_id else [],
        reproduce=f"对比 {origin}/ 与随机不存在路径的 body_sha256/content_length",
        falsify_if=[
            "不同路径返回不同 body_sha256 或明显不同的 content_length",
            "同构响应仅出现在 / 下（业务首页重写），子路径有独立内容",
        ],
        asset_id=origin,
    )
