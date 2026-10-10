"""最小 ReAct 循环测试（FakeLLM 驱动）。"""

from __future__ import annotations

import asyncio

import pytest

from secpt.config.models import ToolsConfig
from secpt.core.llm import FakeLLM, LLMMessage
from secpt.core.react import ReactResult, run_react
from secpt.core.tools.base import Registry
from secpt.core.tools.demo import EchoTool
from secpt.runtime import subprocess as sp
from secpt.runtime.subprocess import run_whitelisted


@pytest.fixture
def echo_registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """提供注册了 EchoTool 的 Registry，并 monkeypatch 放行 echo。"""
    cfg = ToolsConfig(whitelist=["echo"])
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)

    reg = Registry()
    reg.register(EchoTool())
    return reg


@pytest.mark.asyncio
async def test_react_action_then_answer(echo_registry: Registry) -> None:
    """FakeLLM 先 ACTION 后 ANSWER：answer/steps/exhausted 正确。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="ACTION: echo hello"),
            LLMMessage(role="assistant", content="ANSWER: echo 回了 hello"),
        ]
    )
    result = await run_react(llm=llm, tools=echo_registry, task="say hi")
    assert isinstance(result, ReactResult)
    assert result.answer == "echo 回了 hello"
    assert result.steps == 2
    assert result.exhausted is False
    assert any(t.get("type") == "action" and t.get("tool") == "echo" for t in result.trajectory)
    assert result.trajectory[-1].get("type") == "finish"


@pytest.mark.asyncio
async def test_react_unknown_tool(echo_registry: Registry) -> None:
    """未知工具：observation 为 Error，LLM 继续 ANSWER。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="ACTION: nope xyz"),
            LLMMessage(role="assistant", content="ANSWER: gave up"),
        ]
    )
    result = await run_react(llm=llm, tools=echo_registry, task="x")
    assert result.answer == "gave up"
    assert result.steps == 2
    assert result.exhausted is False


@pytest.mark.asyncio
async def test_react_format_error(echo_registry: Registry) -> None:
    """格式错误：runner 返回 Error，LLM 继续。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="随便说一句"),
            LLMMessage(role="assistant", content="ANSWER: done"),
        ]
    )
    result = await run_react(llm=llm, tools=echo_registry, task="x")
    assert result.answer == "done"
    assert result.steps == 2
    assert result.exhausted is False


@pytest.mark.asyncio
async def test_react_exhausted(echo_registry: Registry) -> None:
    """LLM 一直 ACTION：max_steps 耗尽。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="ACTION: echo x"),
            LLMMessage(role="assistant", content="ACTION: echo x"),
            LLMMessage(role="assistant", content="ACTION: echo x"),
        ]
    )
    result = await run_react(llm=llm, tools=echo_registry, task="x", max_steps=3)
    assert result.exhausted is True
    assert result.steps == 3
    assert "未给出结论" in result.answer


@pytest.mark.asyncio
async def test_react_custom_system_prompt(echo_registry: Registry) -> None:
    """自定义 system_prompt 透传。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="ANSWER: pong"),
        ]
    )
    result = await run_react(
        llm=llm,
        tools=echo_registry,
        task="ping",
        system_prompt="custom prompt",
        max_steps=1,
    )
    assert result.answer == "pong"
    assert result.steps == 1
    assert result.exhausted is False


def test_run_whitelisted_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_whitelisted 在 monkeypatch 环境下可用（防回归）。"""
    cfg = ToolsConfig(whitelist=["echo"])
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)

    res = asyncio.run(run_whitelisted("echo", ["ping"]))
    assert res.returncode == 0
