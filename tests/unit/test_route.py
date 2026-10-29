"""M5-22 路由器官测试：任务/对话判定、goal 兜底、答复兜底、错误上抛。"""

from __future__ import annotations

import pytest

from secpt.errors import LlmError
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.route import Route, Router


def test_pentest_route_falls_back_to_raw_text() -> None:
    """判任务但没给 goal：用原文兜底（用户那句话就是任务描述）。"""
    fake = FakeLLM([Route(mode="pentest", goal="  ")])
    route = Router(LlmClient(fake)).decide("扫一下 10.0.0.5 的端口")
    assert route.mode == "pentest"
    assert route.goal == "扫一下 10.0.0.5 的端口"


def test_chat_route_keeps_model_reply() -> None:
    route = Router(LlmClient(FakeLLM([Route(mode="chat", reply="XSS 是跨站脚本。")]))).decide(
        "什么是 XSS"
    )
    assert route.mode == "chat" and route.reply == "XSS 是跨站脚本。"


def test_chat_route_blank_reply_uses_honest_placeholder() -> None:
    route = Router(LlmClient(FakeLLM([Route(mode="chat", reply="   ")]))).decide("在吗")
    assert "模型未给出答复" in route.reply


def test_prompt_carries_user_input() -> None:
    fake = FakeLLM([Route(mode="chat", reply="ok")])
    Router(LlmClient(fake)).decide("请介绍一下这个系统")
    assert "请介绍一下这个系统" in fake.calls[0][0].content


def test_llm_error_propagates() -> None:
    """路由失败不猜模式：LlmError 原样上抛，由 main 兜底成非零退出。"""
    with pytest.raises(LlmError):
        Router(LlmClient(FakeLLM([LlmError("炸了")]))).decide("x")
