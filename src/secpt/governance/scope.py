"""G1：目标授权范围（Scope）校验。

白名单管命令，Scope 管目标。任何工具执行前校验目标是否在授权范围内。
"""

from __future__ import annotations

import ipaddress
import os

import structlog

logger = structlog.get_logger(__name__)


class ScopeError(Exception):
    """目标不在授权范围内。"""

    # duck-typing 拒绝信号：engine 层据此区分"治理拒绝（fail-closed）"与
    # "校验器自身异常（fail-open 放行）"，避免 engine 反向依赖 governance
    scope_denied = True


class ScopeValidator:
    """校验目标是否在授权范围内。

    支持：
    - 精确 IP：192.168.1.1
    - CIDR 网段：192.168.1.0/24
    - 域名：example.com（含子域名 *.example.com）
    """

    def __init__(self, allowed: list[str]) -> None:
        self._allowed = allowed
        self._allowed_ips: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self._allowed_domains: list[str] = []
        self._parse_allowed()

    def _parse_allowed(self) -> None:
        for item in self._allowed:
            try:
                if "/" in item:
                    self._allowed_ips.append(ipaddress.ip_network(item, strict=False))
                else:
                    self._allowed_ips.append(ipaddress.ip_network(item + "/32", strict=False))
            except ValueError:
                # 不是 IP，当域名处理
                self._allowed_domains.append(item.lower().lstrip("*."))

    def is_allowed(self, target: str) -> bool:
        """检查目标是否在授权范围内。"""
        target = target.strip().lower()

        # 尝试 IP 匹配
        try:
            ip = ipaddress.ip_address(target)
            for net in self._allowed_ips:
                if ip in net:
                    return True
        except ValueError:
            pass

        # 域名匹配（含子域名）
        for domain in self._allowed_domains:
            if target == domain or target.endswith("." + domain):
                return True

        return False

    def validate(self, target: str) -> None:
        """校验目标，不在范围内抛 ScopeError。"""
        if not self.is_allowed(target):
            logger.warning("scope.denied", target=target, allowed=self._allowed)
            raise ScopeError(f"目标 {target} 不在授权范围内（Scope: {self._allowed}）")
        logger.debug("scope.allowed", target=target)


def build_scope_validator(entries: list[str] | None = None) -> ScopeValidator | None:
    """装配 ScopeValidator（worker.py / engine_runner.py 装配处调用）。

    白名单来源：TaskSpec.entries（题面抽取的入口）+ 环境变量 SECPT_SCOPE_ALLOW
    （逗号分隔，CIDR/域名/IP 均可）。

    返回 None 的情形（= 不启用 scope 校验，与 SECPT_SCOPE=0 等效）：
    - SECPT_SCOPE=0（全局关闭，与 SECPT_INTERCEPT 对称）
    - entries 与 SECPT_SCOPE_ALLOW 双空：无授权信息无从校验，此时启用会把
      所有网络命令 fail-closed 拒绝、任务瘫痪——宁可不启用，等有入口信息再开
    全放行：SECPT_SCOPE_ALLOW=0.0.0.0/0（显式声明，不做隐式魔法）。
    """
    if os.environ.get("SECPT_SCOPE", "1") == "0":
        return None
    allowed = [e.strip() for e in (entries or []) if e and e.strip()]
    env_allow = os.environ.get("SECPT_SCOPE_ALLOW", "")
    allowed += [a.strip() for a in env_allow.split(",") if a.strip()]
    if not allowed:
        return None
    return ScopeValidator(allowed)


__all__ = ["ScopeError", "ScopeValidator", "build_scope_validator"]
