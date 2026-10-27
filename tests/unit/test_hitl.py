"""权限控制单元测试。"""

import pytest

from harness.security.hitl import (
    ApprovalRequest,
    HITLController,
    auto_approve_callback,
    auto_reject_callback,
)


class TestHITLController:
    async def test_approve_with_callback(self):
        """使用回调批准。"""
        async def callback(request):
            return True

        controller = HITLController(callback)
        approved = await controller.request_approval("delete_file", '{"path": "/tmp/test"}')

        assert approved is True

    async def test_reject_with_callback(self):
        """使用回调拒绝。"""
        async def callback(request):
            return False

        controller = HITLController(callback)
        approved = await controller.request_approval("delete_file", "{}")

        assert approved is False

    async def test_no_callback_defaults_reject(self):
        """无回调时默认拒绝（安全优先）。"""
        controller = HITLController()
        approved = await controller.request_approval("any_tool", "{}")

        assert approved is False

    async def test_history(self):
        """记录审批历史。"""
        async def callback(request):
            return True

        controller = HITLController(callback)
        await controller.request_approval("tool1", "{}")
        await controller.request_approval("tool2", "{}")

        history = controller.get_history()
        assert len(history) == 2

    async def test_auto_approve(self):
        """自动批准回调。"""
        controller = HITLController(auto_approve_callback)
        approved = await controller.request_approval("any", "{}")

        assert approved is True

    async def test_auto_reject(self):
        """自动拒绝回调。"""
        controller = HITLController(auto_reject_callback)
        approved = await controller.request_approval("any", "{}")

        assert approved is False


class TestApprovalRequest:
    def test_request_structure(self):
        req = ApprovalRequest(
            tool_name="delete_file",
            arguments='{"path": "/tmp/test"}',
            reason="危险操作",
        )

        assert req.tool_name == "delete_file"
        assert "path" in req.arguments
        assert req.reason == "危险操作"
