"""LLM 工厂测试：mock 提供商接线、凭证预检、未知提供商报错顺序。"""

import asyncio
import json

import pytest

from adapters.llm.factory import MockLLM, build_llm


class TestMockProvider:
    def test_build_llm_mock_no_credentials_needed(self):
        """LLM_PROVIDER=mock 离线可用：不查凭证、不包韧性层。"""
        assert isinstance(build_llm("mock"), MockLLM)

    def test_mock_chat_triggers_tool_call(self):
        """带 tools 且历史中无工具结果 → 发起 run_command 调用（演示 HITL）。"""
        llm = build_llm("mock")
        tools = [{"type": "function", "function": {"name": "run_command"}}]
        resp = asyncio.run(llm.chat([{"role": "user", "content": "跑个命令"}], tools=tools))
        calls = resp["choices"][0]["message"]["tool_calls"]
        assert calls[0]["function"]["name"] == "run_command"
        assert json.loads(calls[0]["function"]["arguments"])["command"]

    def test_mock_chat_answers_after_tool_result(self):
        """历史中已有工具结果 → 输出基于结果的收尾回答，不再发起调用。"""
        llm = build_llm("mock")
        messages = [
            {"role": "user", "content": "跑个命令"},
            {"role": "tool", "content": "mock-web-demo"},
        ]
        resp = asyncio.run(llm.chat(messages, tools=[]))
        msg = resp["choices"][0]["message"]
        assert msg["tool_calls"] == []
        assert "mock-web-demo" in msg["content"]

    def test_mock_chat_without_tools_is_plain_text(self):
        """不带 tools（路由/总结场景）→ 纯文本确认，避免误触发工具。"""
        llm = build_llm("mock")
        resp = asyncio.run(llm.chat([{"role": "user", "content": "你好"}]))
        msg = resp["choices"][0]["message"]
        assert msg["tool_calls"] == []
        assert "你好" in msg["content"]

    def test_mock_stream_yields_delta_then_final(self):
        async def collect() -> list[dict]:
            llm = build_llm("mock")
            return [ev async for ev in llm.chat_stream([{"role": "user", "content": "hi"}])]

        events = asyncio.run(collect())
        assert events[0]["type"] == "delta"
        assert events[-1]["type"] == "final"
        streamed = "".join(e["content"] for e in events if e["type"] == "delta")
        assert streamed == events[-1]["message"]["content"]


class TestCredentialGuard:
    def test_missing_key_raises_named_error(self, monkeypatch):
        from harness.config import Settings

        monkeypatch.setattr(
            "adapters.llm.factory.get_settings", lambda: Settings(deepseek_api_key="")
        )
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            build_llm("deepseek")

    def test_valid_key_wraps_resilience(self, monkeypatch):
        from harness.config import Settings
        from harness.resilience.llm import ResilientLLM

        monkeypatch.setattr(
            "adapters.llm.factory.get_settings", lambda: Settings(deepseek_api_key="sk-test")
        )
        assert isinstance(build_llm("deepseek"), ResilientLLM)

    def test_unknown_provider_lists_mock(self):
        """未知 provider 报 Unknown provider（而非误导性的缺 API Key）。"""
        with pytest.raises(ValueError, match="Unknown provider"):
            build_llm("nonexistent")
