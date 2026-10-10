"""LLM 调用边界测试。

覆盖：
- FakeLLM 按 script 顺序返回、超出后循环末条并告警
- get_llm(LLMConfig(fake=True)) → FakeLLM
- LiteLLMAdapter 缺 key 抛 LLMError（不依赖网络）
- LiteLLMAdapter 真实调用用 skipif 守卫
"""

from __future__ import annotations

import os

import pytest

from secpt.config.models import LLMConfig
from secpt.core.llm import (
    BaseLLM,
    FakeLLM,
    LiteLLMAdapter,
    LLMError,
    LLMMessage,
    get_llm,
)


@pytest.mark.asyncio
async def test_fake_llm_script_order() -> None:
    """FakeLLM 按 script 顺序返回。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="one"),
            LLMMessage(role="assistant", content="two"),
        ]
    )
    out1 = await llm.acompletion([LLMMessage(role="user", content="hi")])
    out2 = await llm.acompletion([LLMMessage(role="user", content="hi")])
    assert out1.content == "one"
    assert out2.content == "two"


@pytest.mark.asyncio
async def test_fake_llm_script_wraps_last() -> None:
    """script 用完后循环最后一条，并发出 RuntimeWarning 告警。"""
    llm = FakeLLM(script=[LLMMessage(role="assistant", content="only")])

    msgs = [LLMMessage(role="user", content=f"q{i}") for i in range(3)]
    r1 = await llm.acompletion(msgs)
    r2 = await llm.acompletion(msgs)
    r3 = await llm.acompletion(msgs)
    assert r1.content == "only"
    assert r2.content == "only"
    assert r3.content == "only"

    with pytest.warns(RuntimeWarning, match="script exhausted"):
        await llm.acompletion(msgs)


@pytest.mark.asyncio
async def test_fake_llm_generator() -> None:
    """generator 回调按历史生成回复。"""

    def gen(history: list[LLMMessage]) -> LLMMessage:
        last_user = next((m for m in reversed(history) if m.role == "user"), None)
        text = f"echo:{last_user.content if last_user else ''}"
        return LLMMessage(role="assistant", content=text)

    llm = FakeLLM(generator=gen)
    out = await llm.acompletion(
        [LLMMessage(role="user", content="hello"), LLMMessage(role="user", content="world")]
    )
    assert out.content == "echo:world"


def test_get_llm_fake() -> None:
    """fake=True 返回 FakeLLM。"""
    cfg = LLMConfig(fake=True)
    llm = get_llm(cfg)
    assert isinstance(llm, FakeLLM)
    assert isinstance(llm, BaseLLM)


def test_litellm_adapter_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 DEEPSEEK_API_KEY 时抛 LLMError。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = LLMConfig()
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        LiteLLMAdapter(cfg)


def test_litellm_adapter_reads_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """构造时从环境变量读 key，属性可访问。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-abc")
    cfg = LLMConfig()
    adapter = LiteLLMAdapter(cfg)
    assert adapter._api_key == "test-key-abc"


_HAS_DEEPSEEK_KEY = bool(os.environ.get("DEEPSEEK_API_KEY"))


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_DEEPSEEK_KEY, reason="未设置 DEEPSEEK_API_KEY，跳过真实调用")
async def test_litellm_adapter_real_call() -> None:
    """真实 DeepSeek 调用；仅当 DEEPSEEK_API_KEY 存在时执行。"""
    cfg = LLMConfig()
    adapter = LiteLLMAdapter(cfg)
    out = await adapter.acompletion(
        [LLMMessage(role="user", content="Reply with the single word: ok")]
    )
    assert out.role == "assistant"
    assert out.content.strip()
