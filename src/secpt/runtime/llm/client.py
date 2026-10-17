"""LLM 调用内核（多模型路由版）。

在 harness.py 的 call_llm 基础上加固：
- 指数退避重试：429 / 5xx / ReadTimeout / ConnectError / ConnectTimeout / PoolTimeout
  最多重试 4 次，间隔 1/2/4/8s + 抖动（连接类异常此前不重试，内网抖动即整轮失败）；
  429 读 Retry-After 头取 max(指数退避, 服务端指示)（上限 120s）
- 回传 messages 时剔除 reasoning_content（它只落盘审计，放回请求会被拒或污染上下文）
- 超时拆分：read 180s（60s 在长推理时误杀）+ connect 10s（网关宕机快速失败）
- 模块级共享 AsyncClient（按事件循环缓存，连接池复用，不再每请求新建）

多模型路由（OpenAI 兼容端点）：
- ``deepseek/<model>`` 或裸名（如 ``deepseek-chat``）→ api.deepseek.com + DEEPSEEK_API_KEY
- ``openai/<model>`` → OPENAI_BASE_URL（默认官方）+ OPENAI_API_KEY
- 通用自定义端点（任意 OpenAI 兼容网关）：
  ``SECPT_MODEL_BASE_URL`` + ``SECPT_MODEL_NAME`` + ``SECPT_MODEL_API_KEY``
  三者独立覆盖 base_url / model_id / api_key（未设置的项按 provider 规则解析）。
- 自建网关/内网：``SECPT_MODEL_BASE_<PREFIX>`` / ``SECPT_MODEL_KEY_<PREFIX>`` /
  ``SECPT_MODEL_KEYENV_<PREFIX>`` 环境变量注入任意 provider
- 非 OpenAI 兼容协议（如原生 anthropic）需先接入其 OpenAI 兼容端点再用本客户端
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time

from secpt.runtime.metrics import counter_inc, histogram_observe

BASE_URL = "https://api.deepseek.com"
REQUEST_TIMEOUT_S = 180
_CONNECT_TIMEOUT_S = 10.0  # 连接阶段独立超时（网关宕机快速失败进 failover）
_MAX_RETRY_AFTER_S = 120.0  # Retry-After 采纳上限（防异常头部挂死任务）
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

    # 通用自定义端点三件套：任意 OpenAI 兼容网关（vLLM/one-api/内网代理等）。
    # 三者独立覆盖，未设置的项继续走 provider 规则；name 覆盖所有意图/角色模型名。
    base_override = os.environ.get("SECPT_MODEL_BASE_URL", "").strip()
    key_override = os.environ.get("SECPT_MODEL_API_KEY", "").strip()
    name_override = os.environ.get("SECPT_MODEL_NAME", "").strip()

    base_url = base_override or os.environ.get(f"SECPT_MODEL_BASE_{prefix}", base_default)
    key_env = os.environ.get(f"SECPT_MODEL_KEYENV_{prefix}", keyenv_default)
    api_key = (
        key_override or os.environ.get(f"SECPT_MODEL_KEY_{prefix}") or os.environ.get(key_env, "")
    )
    model_id = name_override or rest
    if not api_key:
        raise RuntimeError(
            f"模型 {model!r}（provider {prefix.lower()}）缺少 API key：请设置 "
            f"SECPT_MODEL_API_KEY（通用自定义端点）、{key_env}、"
            f"SECPT_MODEL_KEY_{prefix}，或通过 SECPT_MODEL_KEYENV_{prefix} 指定 key 环境变量"
        )
    return base_url.rstrip("/"), api_key, model_id


def _llm_param(env_name: str, attr: str, default):
    """请求参数解析：环境变量 > 默认（环境变量是唯一配置源，见 docs/系统速览）。

    SECPT_LLM_TEMPERATURE / SECPT_LLM_MAX_TOKENS / SECPT_LLM_TIMEOUT_S
    与 SECPT_MODEL_* 生态一致。attr 参数保留仅为调用点签名兼容。
    """
    v = os.environ.get(env_name)
    if v not in (None, ""):
        return type(default)(v) if default is not None else v
    return default


# 共享 client：按事件循环缓存（asyncio.run 每次新建 loop，不能跨 loop 复用）
_clients: dict = {}


def _get_client() -> object:
    import httpx

    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        timeout_s = float(_llm_param("SECPT_LLM_TIMEOUT_S", "timeout_s", REQUEST_TIMEOUT_S))
        # 超时拆分：connect 独立短超时——网关宕机时快速失败进重试/failover，
        # 而不是每次尝试挂满 180s（read/write 仍按 SECPT_LLM_TIMEOUT_S）
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(_CONNECT_TIMEOUT_S, timeout_s)),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        _clients[loop] = client
    return client


def _retry_after_s(response) -> float | None:
    """解析 429 的 Retry-After 头（秒数或 HTTP-date）；缺失/不可解析 → None。

    采纳上限 _MAX_RETRY_AFTER_S：尊重服务端退避指示，但不允许异常大值挂死任务。
    """
    try:
        raw = (response.headers.get("retry-after") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return min(_MAX_RETRY_AFTER_S, max(0.0, float(raw)))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        return min(_MAX_RETRY_AFTER_S, max(0.0, dt.timestamp() - time.time()))
    except Exception:
        return None


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
        # A：缓存命中可观测——DeepSeek 兼容端点返回 prompt_cache_hit/miss_tokens，
        # 透传给 Engine 记指标（其他端点无此字段时为 0，命中率汇总结论不受影响）
        u.prompt_cache_hit_tokens = int(data["usage"].get("prompt_cache_hit_tokens") or 0)
        u.prompt_cache_miss_tokens = int(data["usage"].get("prompt_cache_miss_tokens") or 0)
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
    # 端点路径：默认 /chat/completions；base 已含该路径时不重复拼接；
    # 自建网关/Azure 可用 SECPT_MODEL_CHAT_PATH 覆盖（如 /openai/deployments/x/chat/completions）
    chat_path = (
        os.environ.get("SECPT_MODEL_CHAT_PATH", "/chat/completions").strip() or "/chat/completions"
    )
    if not chat_path.startswith("/"):
        chat_path = "/" + chat_path
    url = base_url if base_url.endswith(chat_path) else f"{base_url}{chat_path}"

    key = resolved_key if api_key is None else (api_key or resolved_key)
    # 鉴权头可配：默认 Authorization: Bearer <key>；Azure 可用 api-key + 空 scheme
    auth_header = (
        os.environ.get("SECPT_MODEL_AUTH_HEADER", "Authorization").strip() or "Authorization"
    )
    auth_scheme = os.environ.get("SECPT_MODEL_AUTH_SCHEME", "Bearer").strip()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    headers[auth_header] = f"{auth_scheme} {key}".strip() if auth_scheme else key
    # 额外请求头（JSON 对象；如网关要求 X-Tenant）
    extra_raw = os.environ.get("SECPT_MODEL_EXTRA_HEADERS", "").strip()
    if extra_raw:
        try:
            extra = json.loads(extra_raw)
            if not isinstance(extra, dict):
                raise ValueError("必须是 JSON 对象")
            headers.update({str(k): str(v) for k, v in extra.items()})
        except Exception as e:
            raise RuntimeError(f"SECPT_MODEL_EXTRA_HEADERS 解析失败: {e}") from e
    payload: dict = {
        "model": model_id,
        "messages": sanitize_messages(messages),
        "tools": tools,
    }
    temperature = _llm_param("SECPT_LLM_TEMPERATURE", "temperature", None)
    if temperature is not None:
        payload["temperature"] = float(temperature)
    max_tokens = _llm_param("SECPT_LLM_MAX_TOKENS", "max_tokens", None)
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
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
    _t0 = time.perf_counter()

    def _metric(status: str) -> None:
        """指标插桩：llm_calls_total{model,status} + llm_latency_seconds{model}。"""
        counter_inc("llm_calls_total", model=model_id, status=status)
        histogram_observe("llm_latency_seconds", time.perf_counter() - _t0, model=model_id)

    try:
        for attempt in range(MAX_RETRIES + 1):
            try:
                client = _get_client()
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
                resp = _wrap_response(r.json())
                _metric("ok")
                return resp
            except retryable_exc as e:
                retryable = not isinstance(e, httpx.HTTPStatusError)
                if isinstance(e, httpx.HTTPStatusError):
                    code = e.response.status_code
                    retryable = code == 429 or 500 <= code < 600
                if not retryable or attempt == MAX_RETRIES:
                    raise
                last_exc = e
                delay = _BACKOFF_BASE_S * (2**attempt) + random.random()
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
                    ra = _retry_after_s(e.response)
                    if ra is not None:
                        delay = max(delay, ra)  # 尊重服务端指示（不低于指数退避）
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc
    except Exception:
        _metric("error")
        raise
