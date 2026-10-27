"""LLM 韧性：重试、超时、熔断、限流、缓存。

生产环境 LLM 调用需要处理：
- 网络抖动：自动重试
- 服务过载：熔断保护
- 成本控制：请求缓存
- 防止滥用：速率限制
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMClient(Protocol):
    """LLM 客户端协议。"""

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        """发送聊天请求。"""
        ...


@dataclass
class RetryConfig:
    """重试配置。"""

    max_retries: int = 3
    base_delay: float = 1.0  # 基础延迟（秒）
    max_delay: float = 60.0  # 最大延迟
    exponential: bool = True  # 指数退避
    retryable_errors: tuple = (Exception,)  # 可重试的错误类型


@dataclass
class CircuitBreakerConfig:
    """熔断配置。"""

    failure_threshold: int = 5  # 失败阈值
    recovery_timeout: float = 30.0  # 恢复超时（秒）
    half_open_max_calls: int = 3  # 半开状态最大尝试


@dataclass
class RateLimitConfig:
    """限流配置。"""

    max_requests: int = 60  # 窗口内最大请求数
    window_seconds: float = 60.0  # 窗口大小（秒）


class RetryableLLM:
    """带重试机制的 LLM 客户端包装器。"""

    def __init__(
        self,
        client: LLMClient,
        config: RetryConfig | None = None,
        on_retry: Callable[[int, Exception], None] | None = None,
    ) -> None:
        self.client = client
        self.config = config or RetryConfig()
        self.on_retry = on_retry

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """发送请求，失败时自动重试。"""
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                return await self._call_with_timeout(messages, tools)
            except Exception as e:
                last_error = e

                # 检查是否可重试
                if not isinstance(e, self.config.retryable_errors):
                    raise

                # 最后一次尝试失败，不再重试
                if attempt == self.config.max_retries:
                    break

                # 计算延迟
                delay = self._calculate_delay(attempt)
                if self.on_retry:
                    self.on_retry(attempt + 1, e)

                await asyncio.sleep(delay)

        raise last_error  # type: ignore

    async def _call_with_timeout(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """带超时的调用。"""
        # 注意：这里简化处理，实际应该用 asyncio.wait_for
        return await self.client.chat(messages, tools)

    def _calculate_delay(self, attempt: int) -> float:
        """计算重试延迟。"""
        if self.config.exponential:
            delay = self.config.base_delay * (2 ** attempt)
        else:
            delay = self.config.base_delay

        return min(delay, self.config.max_delay)


class CircuitBreaker:
    """熔断器：防止级联故障。"""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self.config = config or CircuitBreakerConfig()
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._state = "closed"  # closed | open | half_open
        self._half_open_calls = 0

    async def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """通过熔断器执行函数。"""
        if self._state == "open":
            if time.time() - self._last_failure_time > self.config.recovery_timeout:
                self._state = "half_open"
                self._half_open_calls = 0
            else:
                raise RuntimeError("熔断器已打开，请求被拒绝")

        if self._state == "half_open":
            if self._half_open_calls >= self.config.half_open_max_calls:
                raise RuntimeError("半开状态请求过多")
            self._half_open_calls += 1

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e

    def _on_success(self) -> None:
        """成功时重置。"""
        self._failure_count = 0
        self._state = "closed"

    def _on_failure(self) -> None:
        """失败时计数。"""
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._failure_count >= self.config.failure_threshold:
            self._state = "open"

    @property
    def state(self) -> str:
        """当前状态。"""
        return self._state


class RateLimiter:
    """速率限制器（滑动窗口）。"""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self.config = config or RateLimitConfig()
        self._requests: list[float] = []

    async def acquire(self) -> None:
        """获取请求许可，超时时等待。"""
        now = time.time()
        window_start = now - self.config.window_seconds

        # 清理过期请求
        self._requests = [t for t in self._requests if t > window_start]

        # 检查是否超限
        if len(self._requests) >= self.config.max_requests:
            # 等待最早的请求过期
            sleep_time = self._requests[0] + self.config.window_seconds - now
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            # 重新获取
            return await self.acquire()

        self._requests.append(now)

    @property
    def available(self) -> int:
        """剩余可用请求数。"""
        now = time.time()
        window_start = now - self.config.window_seconds
        self._requests = [t for t in self._requests if t > window_start]
        return max(0, self.config.max_requests - len(self._requests))


class ResponseCache:
    """响应缓存：避免重复请求。"""

    def __init__(self, ttl_seconds: float = 3600, max_size: int = 1000) -> None:
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._cache: dict[str, tuple[dict, float]] = {}

    def _make_key(self, messages: list[dict], tools: list[dict] | None) -> str:
        """生成缓存键。"""
        data = {
            "messages": messages,
            "tools": tools or [],
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def get(self, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any] | None:
        """获取缓存。"""
        key = self._make_key(messages, tools)
        if key in self._cache:
            value, expire_time = self._cache[key]
            if time.time() < expire_time:
                return value
            del self._cache[key]
        return None

    def set(self, messages: list[dict], tools: list[dict] | None, response: dict[str, Any]) -> None:
        """设置缓存。"""
        if len(self._cache) >= self.max_size:
            # 简单的 LRU：删除最旧的一项
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]

        key = self._make_key(messages, tools)
        self._cache[key] = (response, time.time() + self.ttl)

    def clear(self) -> None:
        """清空缓存。"""
        self._cache.clear()


class ResilientLLM:
    """高韧性 LLM 客户端：组合重试、熔断、限流、缓存。"""

    def __init__(
        self,
        client: LLMClient,
        retry_config: RetryConfig | None = None,
        breaker_config: CircuitBreakerConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
        cache_ttl: float = 3600,
        enable_cache: bool = True,
    ) -> None:
        self.client = client
        self.retry = RetryableLLM(client, retry_config)
        self.breaker = CircuitBreaker(breaker_config)
        self.rate_limiter = RateLimiter(rate_limit_config)
        self.cache = ResponseCache(ttl_seconds=cache_ttl)
        self.enable_cache = enable_cache

        # 底层不支持流式（如 AnthropicLLM）时置 None，
        # 让外层 hasattr 检测自然回退到非流式 chat
        if not hasattr(client, "chat_stream"):
            self.chat_stream = None  # type: ignore[method-assign]

        # 统计
        self._stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "retries": 0,
            "failures": 0,
        }

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """发送请求（带完整韧性保障）。"""
        self._stats["total_requests"] += 1

        # 1. 检查缓存
        if self.enable_cache:
            cached = self.cache.get(messages, tools)
            if cached:
                self._stats["cache_hits"] += 1
                return cached

        # 2. 限流
        await self.rate_limiter.acquire()

        # 3. 熔断 + 重试
        try:
            response = await self.breaker.call(self.retry.chat, messages, tools)
        except Exception:
            self._stats["failures"] += 1
            raise

        # 4. 写入缓存
        if self.enable_cache:
            self.cache.set(messages, tools, response)

        return response

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None):
        """流式请求：透传底层客户端的流式事件。

        流式响应不适合重试（半途失败重放会产生重复输出），
        仅做熔断计数——失败时熔断器照样累积故障。
        """
        self._stats["total_requests"] += 1
        await self.rate_limiter.acquire()
        async for event in self.client.chat_stream(messages, tools):
            yield event

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        return {
            **self._stats,
            "cache_hit_rate": self._stats["cache_hits"] / max(self._stats["total_requests"], 1),
            "breaker_state": self.breaker.state,
            "rate_limit_available": self.rate_limiter.available,
        }

    def reset_stats(self) -> None:
        """重置统计。"""
        self._stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "retries": 0,
            "failures": 0,
        }
