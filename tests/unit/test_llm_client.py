"""LLM 客户端测试：多模型路由（A4）与重试加固（A1.4）。

- resolve_model：provider 前缀 / 裸名默认 / env 注入
- call_llm：连接类异常与 429 重试、4xx 不重试
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from secpt.runtime.llm import client as client_mod
from secpt.runtime.llm.client import call_llm, resolve_model


class TestResolveModel:
    def test_bare_name_defaults_deepseek(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "dk-test")
        monkeypatch.delenv("SECPT_MODEL_BASE_DEEPSEEK", raising=False)
        base, key, mid = resolve_model("deepseek-chat")
        assert base == "https://api.deepseek.com"
        assert key == "dk-test"
        assert mid == "deepseek-chat"

    def test_openai_prefix(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "oa-test")
        base, key, mid = resolve_model("openai/gpt-4o")
        assert base == "https://api.openai.com/v1"
        assert key == "oa-test"
        assert mid == "gpt-4o"

    def test_env_injection(self, monkeypatch):
        monkeypatch.setenv("SECPT_MODEL_BASE_MYGW", "https://gw.internal/v1")
        monkeypatch.setenv("SECPT_MODEL_KEY_MYGW", "gw-key")
        base, key, mid = resolve_model("mygw/qwen3-32b")
        assert base == "https://gw.internal/v1"
        assert key == "gw-key"
        assert mid == "qwen3-32b"

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("SECPT_MODEL_KEY_DEEPSEEK", raising=False)
        with pytest.raises(RuntimeError, match="API key"):
            resolve_model("deepseek-chat")


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


_OK_DATA = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
}


class _FlakyClient:
    """前 N 次抛异常，之后成功。"""

    def __init__(self, failures: list[Exception]):
        self._failures = list(failures)
        self.posts = 0

    async def post(self, url, json=None, headers=None):
        self.posts += 1
        if self._failures:
            raise self._failures.pop(0)
        return _FakeResponse(_OK_DATA)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk-test")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """退避间隔清零（asyncio.sleep(0) 只让出一次，不碰全局 asyncio 模块）。"""
    monkeypatch.setattr(client_mod, "_BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr(client_mod.random, "random", lambda: 0.0)


def test_retry_on_connect_error(monkeypatch):
    fake = _FlakyClient([httpx.ConnectError("conn refused"), httpx.ConnectTimeout("timeout")])
    monkeypatch.setattr(client_mod, "_get_client", lambda: fake)
    resp = _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat", "dk-test"))
    assert resp.choices[0].message.content == "ok"
    assert fake.posts == 3


def test_retry_on_429_then_success(monkeypatch):
    err = httpx.HTTPStatusError(
        "rate limited",
        request=None,
        response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
    )
    fake = _FlakyClient([err])
    monkeypatch.setattr(client_mod, "_get_client", lambda: fake)
    resp = _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat", "dk-test"))
    assert resp.choices[0].message.content == "ok"
    assert fake.posts == 2


def test_400_not_retried(monkeypatch):
    err = httpx.HTTPStatusError(
        "bad request",
        request=None,
        response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
    )
    fake = _FlakyClient([err, err, err, err, err])
    monkeypatch.setattr(client_mod, "_get_client", lambda: fake)
    with pytest.raises(httpx.HTTPStatusError):
        _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat", "dk-test"))
    assert fake.posts == 1  # 4xx 不重试


def test_exhausts_retries(monkeypatch):
    fake = _FlakyClient([httpx.ConnectError(str(i)) for i in range(10)])
    monkeypatch.setattr(client_mod, "_get_client", lambda: fake)
    with pytest.raises(httpx.ConnectError):
        _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat", "dk-test"))
    assert fake.posts == client_mod.MAX_RETRIES + 1
