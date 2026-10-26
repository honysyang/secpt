"""确定性敏感暴露检测（联调裁定）：不依赖模型自觉，规则命中即提案 finding。

背景：真实联调 aimohu.com 时 worker 请求 /.git/config 得到 HTTP 200——
版本库配置可公开读取是教科书级发现，但 v0 里 finding 只能由模型主动
提案，规则引擎（JEV）只裁决不发现，于是这条线索只进了 clue 列表，
findings 恒为 0。敏感路径的可访问性由本模块兜底：worker 每条 http
产出过一遍规则，命中即落 finding.proposed 事件，交 JEV 裁决。

规则刻意保守：只认 2xx/3xx（4xx/5xx 的探测价值由线索本身承载），
模式集是公开漏洞案例里的常客（版本库元数据、环境文件、备份残留）。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from pa.models import FindingProposal

_SENSITIVE_PATH = re.compile(
    r"(\.git/|\.svn/|\.hg/|\.env([./?]|$)|\.aws/|\.ssh/"
    r"|\.bak([./?]|$)|\.old([./?]|$)|\.sql([./?]|$)|\.swp$|~$)",
    re.IGNORECASE)
_HIGH = (".git/", ".svn/", ".env", ".aws/", ".ssh/")


def sensitive_finding(url: str, status: int,
                      evidence_id: str | None) -> FindingProposal | None:
    """命中敏感路径模式且状态为 2xx/3xx → 疑似暴露 finding；否则 None。"""
    if not (200 <= status < 400):
        return None
    path = urlsplit(url).path or "/"
    if not _SENSITIVE_PATH.search(path):
        return None
    sev = "high" if any(p in path.lower() for p in _HIGH) else "med"
    return FindingProposal(
        claim=f"敏感路径 {path} 可公开访问（HTTP {status}，未经认证）",
        severity=sev,
        evidence_refs=[evidence_id] if evidence_id else [],
        reproduce=f"curl -ik {url}",
        falsify_if=["响应实为认证重定向（Location 指向登录页）",
                    "响应是自定义错误页，真实内容需有效会话",
                    "WAF/反代对自动化来源统一返回该状态码"],
        asset_id=None)
