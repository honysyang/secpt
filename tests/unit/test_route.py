"""M5-22 路由器官测试：任务/对话判定、goal 兜底、prompt 携带输入、错误上抛。

路由只负责分类：判 chat 时**不在这里作答**（回答交给带身份的对话器官）。
"""

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


def test_chat_route_carries_no_goal_and_no_answer() -> None:
    """判对话：只给模式，不作答（goal 字段被忽略）。"""
    route = Router(LlmClient(FakeLLM([Route(mode="chat", goal="忽略我")]))).decide("什么是 XSS")
    assert route.mode == "chat"


def test_prompt_carries_user_input() -> None:
    fake = FakeLLM([Route(mode="chat")])
    Router(LlmClient(fake)).decide("请介绍一下这个系统")
    assert "请介绍一下这个系统" in fake.calls[0][0].content


def test_llm_error_propagates() -> None:
    """路由失败不猜模式：LlmError 原样上抛，由 main 兜底成非零退出。"""
    with pytest.raises(LlmError):
        Router(LlmClient(FakeLLM([LlmError("炸了")]))).decide("x")
