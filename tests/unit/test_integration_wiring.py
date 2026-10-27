"""接线回归测试：韧性 / HITL / 上下文预算 / 会话历史 / 黑板持久化。

这五套组件此前"存在但未接入运行链路"，本文件防止它们再次断线。
"""

import pytest

from adapters.llm.factory import build_llm
from harness.tools.registry import ToolRegistry
from runtime.blackboard import HierarchicalBlackboard
from runtime.loop import ExecutionLoop


class TestResilienceWiring:
    """build_llm 必须返回 ResilientLLM（而非裸客户端）。"""

    def test_build_llm_wraps_resilient(self, monkeypatch):
        import adapters.llm.factory as factory

        class FakeUnderlying:
            async def chat(self, messages, tools=None):
                return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-1234567890")
        client = build_llm("deepseek")
        assert type(client).__name__ == "ResilientLLM", (
            "build_llm 必须包 ResilientLLM，否则 429/超时直接炸到用户面板"
        )
        assert hasattr(client, "chat_stream")


class TestHITLWiring:
    """危险工具审批钩子应通过 approval_hook 挂在注册表上并被 loop 使用。"""

    def test_registry_has_approval_hook(self):
        registry = ToolRegistry()
        assert registry.approval_hook is None  # 默认不启用

    async def test_loop_uses_registry_hook(self):
        """loop 无显式 hitl_callback 时，应回退到 registry.approval_hook。"""
        from harness.tools.builtin import register_builtin

        registry = ToolRegistry()
        register_builtin(registry, include_dangerous=True)
        calls: list[str] = []

        async def hook(name: str, args: str) -> bool:
            calls.append(name)
            return False  # 拒绝

        registry.approval_hook = hook

        class FakeLLM:
            def __init__(self):
                self.n = 0

            async def chat(self, messages, tools=None):
                self.n += 1
                if self.n == 1:
                    # 请求危险工具 write_file
                    return {
                        "choices": [{"message": {
                            "content": "",
                            "tool_calls": [{
                                "id": "t1",
                                "type": "function",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path": "evil.txt", "content": "x"}',
                                },
                            }],
                        }}],
                        "usage": {"total_tokens": 1},
                    }
                return {
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {"total_tokens": 1},
                }

        loop = ExecutionLoop(FakeLLM(), registry, max_steps=3)
        result = await loop.run_react("写文件")

        assert calls == ["write_file"], "危险工具必须先经过 approval_hook"
        # 被拒绝 → observation 是拒绝信息，而不是执行结果
        obs = [s["content"] for s in result.steps if s["type"] == "observation"]
        assert any("拒绝" in c or "未批准" in c for c in obs)


class TestContextBudget:
    """上下文超预算时必须裁剪旧轮次，防止消息无限增长。"""

    async def test_trim_keeps_recent_and_system(self):
        from harness.tools.builtin import register_builtin

        registry = ToolRegistry()
        register_builtin(registry)

        # 构造超长消息历史
        messages = [{"role": "system", "content": "sys"}]
        for i in range(20):
            messages.append({"role": "tool", "content": "x" * 10_000})

        loop = ExecutionLoop.__new__(ExecutionLoop)
        loop.context_budget_chars = 60_000
        loop._trim_messages(messages)

        assert messages[0]["content"] == "sys"  # system 保留
        recent = messages[-6:]
        assert all(len(m["content"]) == 10_000 for m in recent)  # 最近消息完整
        trimmed = messages[1:-6]
        assert all(len(m["content"]) <= 530 for m in trimmed)  # 旧的被截断
        assert any("已因上下文预算截断" in m["content"] for m in trimmed)

    def test_run_react_returns_messages(self):
        """LoopResult 必须携带 messages——会话历史依赖它。"""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(
            __import__("runtime.loop", fromlist=["LoopResult"]).LoopResult
        )}
        assert "messages" in fields


class TestBlackboardPersistence:
    """黑板跨会话快照：export/restore 往返一致。"""

    async def test_export_restore_roundtrip(self):
        bb1 = HierarchicalBlackboard()
        await bb1.write_global("goal", "扫描 aimohu.com", agent="tester")
        await bb1.write_critical("asset_ip", "47.110.240.234", agent="tester", reason="资产IP")
        await bb1.write_domain("tasks", "t1", "基线探测完成", agent="tester")

        data = bb1.export_state()
        assert "goal" in data["global"]

        bb2 = HierarchicalBlackboard()
        count = bb2.restore_state(data)
        assert count == 3

        assert (await bb2.read_global("goal")) == "扫描 aimohu.com"
        # write_critical 以 critical_ 前缀 + {data, reason} 包装存储
        assert (await bb2.read_global("critical_asset_ip"))["data"] == "47.110.240.234"
        assert (await bb2.read_domain("tasks", "t1")) == "基线探测完成"

    async def test_expired_entries_not_exported(self):
        bb = HierarchicalBlackboard()
        await bb.write_global("tmp", "will expire", ttl=0.01)
        import asyncio as _aio
        await _aio.sleep(0.02)
        data = bb.export_state()
        assert "tmp" not in data["global"], "过期条目不应进入快照"
