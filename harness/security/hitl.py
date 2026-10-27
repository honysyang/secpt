"""权限控制：人机协同（HITL）确认机制。

对危险操作进行人工确认，防止误操作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Awaitable


@dataclass
class ApprovalRequest:
    """审批请求。"""

    tool_name: str
    arguments: str
    reason: str = ""


class HITLController:
    """人机协同控制器。

    对标记为 dangerous 的工具调用进行人工确认。
    """

    def __init__(
        self,
        callback: Callable[[ApprovalRequest], Awaitable[bool]] | None = None,
    ) -> None:
        """初始化。

        Args:
            callback: 审批回调函数，返回 True 表示批准
        """
        self.callback = callback
        self._pending: dict[str, ApprovalRequest] = {}
        self._history: list[tuple[ApprovalRequest, bool]] = []

    async def request_approval(self, tool_name: str, arguments: str) -> bool:
        """请求人工确认。

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            是否批准
        """
        request = ApprovalRequest(
            tool_name=tool_name,
            arguments=arguments,
        )

        if self.callback is None:
            # 无回调时默认拒绝（安全优先）
            return False

        approved = await self.callback(request)
        self._history.append((request, approved))
        return approved

    def get_history(self) -> list[tuple[ApprovalRequest, bool]]:
        """获取审批历史。"""
        return self._history.copy()

    def clear_history(self) -> None:
        """清空审批历史。"""
        self._history.clear()


def console_approval_callback(request: ApprovalRequest) -> bool:
    """控制台审批回调（同步版本）。"""
    print(f"\n{'='*40}")
    print(f"⚠️  危险操作确认")
    print(f"{'='*40}")
    print(f"工具: {request.tool_name}")
    print(f"参数: {request.arguments}")
    print(f"{'='*40}")

    response = input("是否执行? (y/n): ").strip().lower()
    return response in ("y", "yes", "是")


async def auto_approve_callback(request: ApprovalRequest) -> bool:
    """自动批准回调（用于测试/可信环境）。"""
    return True


async def auto_reject_callback(request: ApprovalRequest) -> bool:
    """自动拒绝回调（用于高安全环境）。"""
    return False
