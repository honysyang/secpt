"""真实 DeepSeek 冒烟测试。

无 DEEPSEEK_API_KEY 时全部跳过；设置后：
- test_adapter_real：直接调 acompletion
- test_run_react_real：跑完整 run_react 用 echo 工具
"""

from __future__ import annotations

import os

import pytest

HAS_KEY = bool(os.getenv("DEEPSEEK_API_KEY"))


@pytest.fixture
def echo_registry(monkeypatch: pytest.MonkeyPatch):
    """放行 echo 白名单 + 注册 EchoTool。"""
    from secpt.config.models import ToolsConfig
    from secpt.config.settings import get_settings
    from secpt.core.tools.base import Registry
    from secpt.core.tools.demo import EchoTool
    from secpt.runtime import subprocess as sp

    monkeypatch.setenv("SECPT_TOOLS__WHITELIST", '["echo"]')
    get_settings.cache_clear()

    cfg = ToolsConfig(whitelist=["echo"])
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)

    reg = Registry()
    reg.register(EchoTool())
    return reg


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_KEY, reason="needs DEEPSEEK_API_KEY")
async def test_adapter_real() -> None:
    """真实 DeepSeek 调用：acompletion 返回非空 content。"""
    from secpt.config.settings import get_settings
    from secpt.core.llm import LLMMessage, get_llm

    llm = get_llm(get_settings().llm)  # fake=False
    resp = await llm.acompletion([LLMMessage(role="user", content="用一句话解释端口扫描")])
    assert resp.role == "assistant"
    assert resp.content.strip()


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_KEY, reason="needs DEEPSEEK_API_KEY")
async def test_run_react_real(echo_registry) -> None:
    """真实 DeepSeek + run_react：跑通 ACTION → observation → ANSWER。"""
    from secpt.config.settings import get_settings
    from secpt.core.llm import get_llm
    from secpt.core.react.runner import run_react

    llm = get_llm(get_settings().llm)  # fake=False
    result = await run_react(
        llm=llm,
        tools=echo_registry,
        task="用 echo 输出 hello",
        max_steps=4,
    )
    assert result.exhausted is False
    assert result.answer.strip()
    assert any(t.get("type") == "action" for t in result.trajectory)
