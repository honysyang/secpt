"""G3 + G4：高危动作人工确认 + 破坏性动作分级。

高危工具（如 nikto 爆破）执行前需人工确认。
每个工具标注 risk_level：readonly / low / medium / high / destructive。
"""

from __future__ import annotations

from enum import Enum

import structlog

logger = structlog.get_logger(__name__)


class RiskLevel(str, Enum):
    READONLY = "readonly"  # 只读，无副作用
    LOW = "low"  # 轻微副作用（日志、缓存）
    MEDIUM = "medium"  # 可能有副作用（扫描触发 IPS）
    HIGH = "high"  # 显著副作用（爆破、枚举）
    DESTRUCTIVE = "destructive"  # 破坏性（删除、修改）


# 工具风险分级
_TOOL_RISK: dict[str, RiskLevel] = {
    "echo": RiskLevel.READONLY,
    "ping": RiskLevel.READONLY,
    "dig": RiskLevel.READONLY,
    "nslookup": RiskLevel.READONLY,
    "whois": RiskLevel.READONLY,
    "traceroute": RiskLevel.READONLY,
    "curl": RiskLevel.READONLY,  # GET 请求视为只读
    "nmap": RiskLevel.MEDIUM,  # 扫描可能触发 IPS
    "whatweb": RiskLevel.MEDIUM,
    "nikto": RiskLevel.HIGH,  # 主动漏洞扫描
}

# 需要人工确认的风险级别
_CONFIRM_REQUIRED = {RiskLevel.HIGH, RiskLevel.DESTRUCTIVE}


class ConfirmationRequired(Exception):
    """高危动作需要人工确认。"""


class RiskManager:
    """工具风险分级与确认管理。"""

    def get_risk(self, tool_name: str) -> RiskLevel:
        """获取工具风险级别，默认 readonly。"""
        return _TOOL_RISK.get(tool_name, RiskLevel.READONLY)

    def needs_confirmation(self, tool_name: str) -> bool:
        """判断是否需要人工确认。"""
        return self.get_risk(tool_name) in _CONFIRM_REQUIRED

    def confirm(self, tool_name: str, args: str, approved: bool = False) -> None:
        """校验高危动作是否已确认，未确认抛异常。"""
        risk = self.get_risk(tool_name)
        if risk in _CONFIRM_REQUIRED and not approved:
            logger.warning("risk.confirm_required", tool=tool_name, args=args[:60], risk=risk.value)
            raise ConfirmationRequired(
                f"工具 {tool_name} 风险级别 {risk.value}，需要人工确认。参数：{args[:80]}"
            )
        if approved:
            logger.info("risk.confirmed", tool=tool_name, args=args[:60], risk=risk.value)


__all__ = ["ConfirmationRequired", "RiskLevel", "RiskManager"]
