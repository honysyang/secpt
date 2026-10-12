"""LLM 调用内核（多模型路由版）。

在 harness.py 的 call_llm 基础上加固：
- 指数退避重试：429 / 5xx / ReadTimeout / ConnectError / ConnectTimeout / PoolTimeout
  最多重试 4 次，间隔 1/2/4/8s + 抖动（连接类异常此前不重试，内网抖动即整轮失败）
- 回传 messages 时剔除 reasoning_content（它只落盘审计，放回请求会被拒或污染上下文）
- 单次请求 timeout 180s（60s 在长推理时误杀）
- 模块级共享 AsyncClient（按事件循环缓存，连接池复用，不再每请求新建）

多模型路由（OpenAI 兼容端点）：
- ``deepseek/<model>`` 或裸名（如 ``deepseek-chat``）→ api.deepseek.com + DEEPSEEK_API_KEY
- ``openai/<model>`` → OPENAI_BASE_URL（默认官方）+ OPENAI_API_KEY
- 自建网关/内网：``SECPT_MODEL_BASE_<PREFIX>`` / ``SECPT_MODEL_KEY_<PREFIX>`` /
  ``SECPT_MODEL_KEYENV_<PREFIX>`` 环境变量注入任意 provider
- 非 OpenAI 兼容协议（如原生 anthropic）需先接入其 OpenAI 兼容端点再用本客户端
"""

from __future__ import annotations

import asyncio
import os
import random

BASE_URL = "https://api.deepseek.com"
REQUEST_TIMEOUT_S = 180
MAX_RETRIES = 4  # 可重试异常最多重试次数
_BACKOFF_BASE_S = 1.0  # 1/2/4/8 秒

# provider 前缀 → (默认 base_url, 默认 key 环境变量)
_PROVIDERS: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}

# 兼容旧调用：裸模型名默认归属的 provider
_DEFAULT_PROVIDER = "deepseek"


def resolve_model(model: str) -> tuple[str, str, str]:
    """把模型名解析为 (base_url, api_key, model_id)。

    解析顺序：env 注入（SECPT_MODEL_*_<PREFIX>）> provider 前缀 > 裸名默认 deepseek。
    :raises RuntimeError: 找不到 API key
    """
    prefix, _, rest = model.partition("/")
    if not rest:
        prefix, rest = _DEFAULT_PROVIDER, prefix
    prefix = prefix.upper().replace("-", "_")
    base_default, keyenv_default = _PROVIDERS.get(
        prefix.lower(), (_PROVIDERS[_DEFAULT_PROVIDER][0], _PROVIDERS[_DEFAULT_PROVIDER][1])
    )

    base_url = os.environ.get(f"SECPT_MODEL_BASE_{prefix}", base_default)
    key_env = os.environ.get(f"SECPT_MODEL_KEYENV_{prefix}", keyenv_default)
    api_key = os.environ.get(f"SECPT_MODEL_KEY_{prefix}") or os.environ.get(key_env, "")
    if not api_key:
        raise RuntimeError(
            f"模型 {model!r}（provider {prefix.lower()}）缺少 API key："
            f"请 export {key_env}=<key>，或设置 SECPT_MODEL_KEY_{prefix}"
        )
    return base_url.rstrip("/"), api_key, rest


# 共享 client：按事件循环缓存（asyncio.run 每次新建 loop，不能跨 loop 复用）
_clients: dict = {}


def _get_client() -> object:
    import httpx

    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        _clients[loop] = client
    return client


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """剔除 reasoning_content，供下一轮请求使用。"""
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append({k: v for k, v in m.items() if k != "reasoning_content"})
        else:
            out.append(m)
    return out


def _wrap_response(data: dict) -> object:
    """统一成类 openai 响应格式（与 harness.py 相同的轻量对象）。"""
    choice = data["choices"][0]
    msg = choice["message"]
    result = {
        "role": "assistant",
        "content": msg.get("content") or "",
    }
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    if msg.get("reasoning_content"):
        result["reasoning_content"] = msg["reasoning_content"]

    class _Msg:
        pass

    m = _Msg()
    m.content = result["content"]
    m.tool_calls = []
    if result.get("tool_calls"):
        for tc in result["tool_calls"]:
            t = _Msg()
            t.id = tc["id"]
            t.function = _Msg()
            t.function.name = tc["function"]["name"]
            t.function.arguments = tc["function"]["arguments"]
            m.tool_calls.append(t)
    m.reasoning_content = result.get("reasoning_content")

    class _Resp:
        pass

    resp = _Resp()
    resp.choices = [type("c", (), {"message": m})()]
    # S3：透传 usage，供 Engine 预算记账（无 usage 时 Engine 回退估算）
    if isinstance(data.get("usage"), dict):

        class _Usage:
            pass

        u = _Usage()
        u.prompt_tokens = int(data["usage"].get("prompt_tokens") or 0)
        u.completion_tokens = int(data["usage"].get("completion_tokens") or 0)
        resp.usage = u
    return resp


async def call_llm(
    messages: list,
    tools: list,
    model: str,
    api_key: str | None = None,
    fallback_model: str | None = None,
) -> object:
    """调 OpenAI 兼容 chat/completions。api_key 参数仅兼容旧签名，实际按模型解析。"""
    import httpx

    base_url, resolved_key, model_id = resolve_model(model)
    is_reasoner = model_id == (fallback_model or "")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {resolved_key if api_key is None else (api_key or resolved_key)}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": model_id,
        "messages": sanitize_messages(messages),
        "tools": tools,
    }
    # reasoning_content 为 DeepSeek reasoner 专有参数
    if is_reasoner and "deepseek" in base_url:
        payload["include_reasoning_content"] = True

    retryable_exc = (
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.PoolTimeout,
        httpx.HTTPStatusError,
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            client = _get_client()
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return _wrap_response(r.json())
        except retryable_exc as e:
            retryable = not isinstance(e, httpx.HTTPStatusError)
            if isinstance(e, httpx.HTTPStatusError):
                code = e.response.status_code
                retryable = code == 429 or 500 <= code < 600
            if not retryable or attempt == MAX_RETRIES:
                raise
            last_exc = e
            delay = _BACKOFF_BASE_S * (2**attempt) + random.random()
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
