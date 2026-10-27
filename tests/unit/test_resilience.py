"""LLM 韧性单元测试。"""

import asyncio

import pytest

from harness.resilience.llm import (
    CircuitBreaker,
    CircuitBreakerConfig,
    RateLimitConfig,
    RateLimiter,
    ResilientLLM,
    ResponseCache,
    RetryConfig,
    RetryableLLM,
)


class FakeLLM:
    """模拟 LLM，可配置失败次数。"""

    def __init__(self, fail_times: int = 0, response: str = "success"):
        self.fail_times = fail_times
        self.response = response
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise ConnectionError("网络错误")
        return {
            "choices": [{"message": {"content": self.response}}],
            "usage": {"total_tokens": 10},
        }


class TestRetryableLLM:
    async def test_success_first_try(self):
        llm = FakeLLM(fail_times=0)
        retry_llm = RetryableLLM(llm)

        result = await retry_llm.chat([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "success"
        assert llm.call_count == 1

    async def test_retry_on_failure(self):
        llm = FakeLLM(fail_times=2)
        retry_llm = RetryableLLM(llm, RetryConfig(max_retries=3, base_delay=0.01))

        result = await retry_llm.chat([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "success"
        assert llm.call_count == 3  # 2 次失败 + 1 次成功

    async def test_retry_exhausted(self):
        llm = FakeLLM(fail_times=10)
        retry_llm = RetryableLLM(llm, RetryConfig(max_retries=2, base_delay=0.01))

        with pytest.raises(ConnectionError):
            await retry_llm.chat([{"role": "user", "content": "test"}])

        assert llm.call_count == 3  # 初始 + 2 次重试


class TestCircuitBreaker:
    async def test_closed_state(self):
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        llm = FakeLLM()

        # 连续成功
        for _ in range(5):
            await breaker.call(llm.chat, [{"role": "user", "content": "test"}])

        assert breaker.state == "closed"

    async def test_open_after_failures(self):
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        llm = FakeLLM(fail_times=10)

        # 连续失败
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await breaker.call(llm.chat, [{"role": "user", "content": "test"}])

        assert breaker.state == "open"

        # 后续请求被拒绝
        with pytest.raises(RuntimeError, match="熔断器已打开"):
            await breaker.call(llm.chat, [{"role": "user", "content": "test"}])

    async def test_half_open_recovery(self):
        breaker = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=2, recovery_timeout=0.1)
        )
        llm = FakeLLM(fail_times=2)

        # 触发熔断
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await breaker.call(llm.chat, [{"role": "user", "content": "test"}])

        assert breaker.state == "open"

        # 等待恢复
        await asyncio.sleep(0.2)

        # 半开状态，成功调用后关闭
        llm2 = FakeLLM(fail_times=0)
        await breaker.call(llm2.chat, [{"role": "user", "content": "test"}])
        assert breaker.state == "closed"


class TestRateLimiter:
    async def test_under_limit(self):
        limiter = RateLimiter(RateLimitConfig(max_requests=5, window_seconds=1))

        for _ in range(5):
            await limiter.acquire()

        assert limiter.available == 0

    async def test_wait_when_exceeded(self):
        limiter = RateLimiter(RateLimitConfig(max_requests=2, window_seconds=0.1))

        # 快速使用完额度
        await limiter.acquire()
        await limiter.acquire()

        # 第三次需要等待
        start = asyncio.get_event_loop().time()
        await limiter.acquire()
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed >= 0.1  # 等待了窗口时间


class TestResponseCache:
    def test_set_and_get(self):
        cache = ResponseCache()
        messages = [{"role": "user", "content": "test"}]
        response = {"choices": [{"message": {"content": "cached"}}]}

        cache.set(messages, None, response)
        result = cache.get(messages, None)

        assert result == response

    def test_cache_miss(self):
        cache = ResponseCache()
        messages = [{"role": "user", "content": "test"}]

        result = cache.get(messages, None)
        assert result is None

    def test_cache_expiry(self):
        cache = ResponseCache(ttl_seconds=0.1)
        messages = [{"role": "user", "content": "test"}]
        response = {"choices": [{"message": {"content": "cached"}}]}

        cache.set(messages, None, response)

        # 等待过期
        import time
        time.sleep(0.2)

        result = cache.get(messages, None)
        assert result is None

    def test_different_messages_different_keys(self):
        cache = ResponseCache()
        msg1 = [{"role": "user", "content": "test1"}]
        msg2 = [{"role": "user", "content": "test2"}]

        cache.set(msg1, None, {"result": 1})
        cache.set(msg2, None, {"result": 2})

        assert cache.get(msg1, None)["result"] == 1
        assert cache.get(msg2, None)["result"] == 2


class TestResilientLLM:
    async def test_full_stack(self):
        llm = FakeLLM(fail_times=1)
        resilient = ResilientLLM(
            llm,
            retry_config=RetryConfig(max_retries=2, base_delay=0.01),
            breaker_config=CircuitBreakerConfig(failure_threshold=3),
            rate_limit_config=RateLimitConfig(max_requests=10, window_seconds=1),
            cache_ttl=1,
        )

        messages = [{"role": "user", "content": "test"}]

        # 第一次：重试后成功
        result = await resilient.chat(messages)
        assert result["choices"][0]["message"]["content"] == "success"

        # 第二次：命中缓存
        result2 = await resilient.chat(messages)
        assert result2 == result

        # 验证统计
        stats = resilient.get_stats()
        assert stats["total_requests"] == 2
        assert stats["cache_hits"] == 1
        # failures 统计的是最终失败的请求，重试成功的不算失败
        assert stats["failures"] == 0

    async def test_cache_disabled(self):
        llm = FakeLLM()
        resilient = ResilientLLM(llm, enable_cache=False)

        messages = [{"role": "user", "content": "test"}]

        await resilient.chat(messages)
        await resilient.chat(messages)

        stats = resilient.get_stats()
        assert stats["cache_hits"] == 0
        assert llm.call_count == 2
