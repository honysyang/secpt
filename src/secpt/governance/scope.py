"""G1：目标授权范围（Scope）校验。

白名单管命令，Scope 管目标。任何工具执行前校验目标是否在授权范围内。
"""

from __future__ import annotations

import ipaddress
import structlog

logger = structlog.get_logger(__name__)


class ScopeError(Exception):
    """目标不在授权范围内。"""


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


__all__ = ["ScopeError", "ScopeValidator"]
