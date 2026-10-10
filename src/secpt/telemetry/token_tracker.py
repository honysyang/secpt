"""Token 追踪器：记录每次 LLM 调用的 token 消耗。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class TokenUsage:
    """单次 LLM 调用的 token 用量。"""

    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float = 0.0


@dataclass
class TokenStats:
    """累计统计。"""

    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    by_model: dict[str, TokenUsage] = field(default_factory=dict)


class TokenTracker:
    """线程安全的 token 追踪器。"""

    # DeepSeek 定价（每 1M tokens，2024-12 约）
    _PRICE = {
        "deepseek-chat": {"prompt": 0.14, "completion": 0.28},
        "deepseek-reasoner": {"prompt": 0.55, "completion": 2.19},
        "deepseek-v1-flash": {"prompt": 0.07, "completion": 0.14},
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats = TokenStats()
        self._history: list[TokenUsage] = []

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """记录一次调用。"""
        total = prompt_tokens + completion_tokens
        # 估算成本
        price = self._PRICE.get(model, {"prompt": 0.14, "completion": 0.28})
        cost = (prompt_tokens / 1_000_000) * price["prompt"] + \
               (completion_tokens / 1_000_000) * price["completion"]

        usage = TokenUsage(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            cost_usd=cost,
        )

        with self._lock:
            self._stats.total_calls += 1
            self._stats.total_prompt_tokens += prompt_tokens
            self._stats.total_completion_tokens += completion_tokens
            self._stats.total_tokens += total
            self._stats.total_cost_usd += cost
            self._history.append(usage)

            # 按模型累计
            if model not in self._stats.by_model:
                self._stats.by_model[model] = TokenUsage(
                    model=model, prompt_tokens=0, completion_tokens=0, total_tokens=0
                )
            m = self._stats.by_model[model]
            m.prompt_tokens += prompt_tokens
            m.completion_tokens += completion_tokens
            m.total_tokens += total
            m.cost_usd += cost

        logger.debug(
            "token.record",
            model=model,
            prompt=prompt_tokens,
            completion=completion_tokens,
            cost=f"${cost:.6f}",
        )

    def get_stats(self) -> TokenStats:
        """获取当前统计。"""
        with self._lock:
            return TokenStats(
                total_calls=self._stats.total_calls,
                total_prompt_tokens=self._stats.total_prompt_tokens,
                total_completion_tokens=self._stats.total_completion_tokens,
                total_tokens=self._stats.total_tokens,
                total_cost_usd=self._stats.total_cost_usd,
                by_model=dict(self._stats.by_model),
            )

    def summary(self) -> dict:
        """人类可读摘要。"""
        s = self.get_stats()
        return {
            "total_calls": s.total_calls,
            "prompt_tokens": s.total_prompt_tokens,
            "completion_tokens": s.total_completion_tokens,
            "total_tokens": s.total_tokens,
            "estimated_cost_usd": round(s.total_cost_usd, 4),
            "by_model": {
                m: {
                    "tokens": u.total_tokens,
                    "cost_usd": round(u.cost_usd, 4),
                }
                for m, u in s.by_model.items()
            },
        }


__all__ = ["TokenTracker", "TokenUsage", "TokenStats"]
