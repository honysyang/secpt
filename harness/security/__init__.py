"""安全模块：人机协同审批（HITL，Human-in-the-Loop）。"""

from harness.security.hitl import (
    ApprovalRequest,
    HITLController,
    auto_approve_callback,
    auto_reject_callback,
    console_approval_callback,
)

__all__ = [
    "ApprovalRequest",
    "HITLController",
    "auto_approve_callback",
    "auto_reject_callback",
    "console_approval_callback",
]
