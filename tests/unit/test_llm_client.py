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
    @pytest.fixture(autouse=True)
    def _clear_custom_endpoint(self, monkeypatch):
        for k in ("SECPT_MODEL_BASE_URL", "SECPT_MODEL_NAME", "SECPT_MODEL_API_KEY"):
            monkeypatch.delenv(k, raising=False)

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

    def test_custom_endpoint_trio(self, monkeypatch):
        """通用自定义端点：base_url + model 名 + key 三者独立覆盖。"""
        monkeypatch.setenv("SECPT_MODEL_BASE_URL", "https://gw.internal/v1")
        monkeypatch.setenv("SECPT_MODEL_NAME", "qwen3-32b")
        monkeypatch.setenv("SECPT_MODEL_API_KEY", "gw-key")
        base, key, mid = resolve_model("deepseek-chat")
        assert base == "https://gw.internal/v1"
        assert key == "gw-key"
        assert mid == "qwen3-32b"

    def test_custom_name_only_keeps_provider(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "dk-test")
        monkeypatch.setenv("SECPT_MODEL_NAME", "deepseek-v4-flash")
        base, key, mid = resolve_model("deepseek-chat")
        assert base == "https://api.deepseek.com"
        assert key == "dk-test"
        assert mid == "deepseek-v4-flash"

    def test_custom_key_only_keeps_provider_and_name(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("SECPT_MODEL_API_KEY", "generic-key")
        base, key, mid = resolve_model("deepseek-chat")
        assert base == "https://api.deepseek.com"
        assert key == "generic-key"
        assert mid == "deepseek-chat"

    def test_missing_key_message_mentions_custom_var(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SECPT_MODEL_API_KEY"):
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


@pytest.fixture(autouse=True)
def _clean_custom_endpoint(monkeypatch):
    for k in (
        "SECPT_MODEL_BASE_URL",
        "SECPT_MODEL_NAME",
        "SECPT_MODEL_API_KEY",
        "SECPT_MODEL_AUTH_HEADER",
        "SECPT_MODEL_AUTH_SCHEME",
        "SECPT_MODEL_EXTRA_HEADERS",
        "SECPT_MODEL_CHAT_PATH",
    ):
        monkeypatch.delenv(k, raising=False)


class TestCustomEndpointRequest:
    def test_custom_headers_and_chat_path(self, monkeypatch):
        import secpt.runtime.llm.client as m

        monkeypatch.setenv("SECPT_MODEL_BASE_URL", "https://gw.internal/v1")
        monkeypatch.setenv("SECPT_MODEL_NAME", "qwen3-32b")
        monkeypatch.setenv("SECPT_MODEL_API_KEY", "gw-key")
        monkeypatch.setenv("SECPT_MODEL_AUTH_HEADER", "api-key")
        monkeypatch.setenv("SECPT_MODEL_AUTH_SCHEME", "")
        monkeypatch.setenv("SECPT_MODEL_EXTRA_HEADERS", '{"X-Tenant": "t1"}')
        monkeypatch.setenv("SECPT_MODEL_CHAT_PATH", "/custom/chat")
        captured: dict = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        class _Client:
            async def post(self, url, json=None, headers=None):
                captured.update({"url": url, "json": json, "headers": headers})
                return _Resp()

        monkeypatch.setattr(m, "_get_client", lambda: _Client())
        _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat"))
        assert captured["url"] == "https://gw.internal/v1/custom/chat"
        assert captured["headers"]["api-key"] == "gw-key"  # 空 scheme = 原始 key
        assert captured["headers"]["X-Tenant"] == "t1"
        assert captured["json"]["model"] == "qwen3-32b"

    def test_base_already_has_chat_completions(self, monkeypatch):
        import secpt.runtime.llm.client as m

        monkeypatch.setenv("SECPT_MODEL_BASE_URL", "https://gw.internal/v1/chat/completions")
        monkeypatch.setenv("SECPT_MODEL_API_KEY", "k")
        captured: dict = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        class _Client:
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                return _Resp()

        monkeypatch.setattr(m, "_get_client", lambda: _Client())
        _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat"))
        assert captured["url"] == "https://gw.internal/v1/chat/completions"  # 不重复拼接


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


class TestRequestParams:
    def test_temperature_max_tokens_env(self, monkeypatch):
        """SECPT_LLM_TEMPERATURE/MAX_TOKENS 进入请求 payload（mock 传输层）。"""
        import secpt.runtime.llm.client as m

        monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
        monkeypatch.setenv("SECPT_LLM_TEMPERATURE", "0.7")
        monkeypatch.setenv("SECPT_LLM_MAX_TOKENS", "4096")
        captured = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        class _Client:
            async def post(self, url, json=None, headers=None):
                captured.update(json or {})
                return _Resp()

        monkeypatch.setattr(m, "_get_client", lambda: _Client())
        _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat", "dk"))
        assert captured["temperature"] == 0.7
        assert captured["max_tokens"] == 4096

    def test_no_params_by_default(self, monkeypatch):
        import secpt.runtime.llm.client as m

        monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
        for v in ("SECPT_LLM_TEMPERATURE", "SECPT_LLM_MAX_TOKENS"):
            monkeypatch.delenv(v, raising=False)
        # 环境变量是唯一配置源：不配则参数不进 payload（用服务端默认）
        captured = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        class _Client:
            async def post(self, url, json=None, headers=None):
                captured.update(json or {})
                return _Resp()

        monkeypatch.setattr(m, "_get_client", lambda: _Client())
        _run(call_llm([{"role": "user", "content": "hi"}], [], "deepseek-chat", "dk"))
        assert "temperature" not in captured
        assert "max_tokens" not in captured

    def test_base_url_override_for_default_provider(self, monkeypatch):
        """默认 deepseek 的 base_url 也可被 env 覆盖（自建网关场景）。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "dk")
        monkeypatch.setenv("SECPT_MODEL_BASE_DEEPSEEK", "https://gw.internal/v1")
        base, _key, mid = resolve_model("deepseek-chat")
        assert base == "https://gw.internal/v1" and mid == "deepseek-chat"


def test_usage_cache_tokens_passthrough():
    """A：DeepSeek usage 的缓存命中/未命中 token 透传（命中率可观测的前提）。"""
    data = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        },
    }
    resp = client_mod._wrap_response(data)
    assert resp.usage.prompt_cache_hit_tokens == 80
    assert resp.usage.prompt_cache_miss_tokens == 20
    # 无该字段的端点 → 0（不炸）
    resp2 = client_mod._wrap_response(
        {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 1}}
    )
    assert resp2.usage.prompt_cache_hit_tokens == 0
    assert resp2.usage.prompt_cache_miss_tokens == 0
