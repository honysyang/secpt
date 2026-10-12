"""S3 预算追踪 BudgetTracker。

累计 prompt_tokens / completion_tokens / cost_cny / wall_s。
单价从环境变量读取（per 1K input/output），不写死：
SECPT_PRICING_INPUT_PER_1K（默认 0.002）/ SECPT_PRICING_OUTPUT_PER_1K（默认 0.008）。

阈值行为：
- 达到 warn_pct（默认 80%）→ check() 返回 "warn"，由 Engine 往 messages
  追加一条 user 引导消息（只触发一次）
- 达到 100% → check() 返回 "exceeded"，由 Engine 优雅收尾：
  先落 findings + 阶段性 report.md，status=budget_exceeded，不抛异常
"""

from __future__ import annotations

import os
import time


def _pricing() -> tuple[float, float]:
    """token 单价（CNY/1K）：env 可覆盖，默认 deepseek-chat 价。"""
    inp = float(os.environ.get("SECPT_PRICING_INPUT_PER_1K", "0.002"))
    out = float(os.environ.get("SECPT_PRICING_OUTPUT_PER_1K", "0.008"))
    return inp, out


WARN_80_MSG = "预算已用 80%。请停止探索，把你已确认的事实整理成结构化结论输出。"


class BudgetTracker:
    def __init__(
        self,
        cost_limit_cny: float = 0.0,
        *,
        warn_pct: float = 0.8,
        input_per_1k: float | None = None,
        output_per_1k: float | None = None,
    ) -> None:
        _inp, _out = _pricing()
        self.input_per_1k = _inp if input_per_1k is None else input_per_1k
        self.output_per_1k = _out if output_per_1k is None else output_per_1k
        self.cost_limit_cny = float(cost_limit_cny or 0.0)
        self.warn_pct = float(warn_pct)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_cny = 0.0
        self.wall_s = 0.0
        self.warned = False
        self.exceeded = False
        self._t0 = time.time()

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        """累计一次 LLM 调用的 token 消耗。"""
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self.cost_cny = (
            self.prompt_tokens / 1000.0 * self.input_per_1k
            + self.completion_tokens / 1000.0 * self.output_per_1k
        )
        self.wall_s = time.time() - self._t0

    def percent(self) -> float:
        """预算已用百分比（0-1+）；无成本上限时恒为 0。"""
        if self.cost_limit_cny <= 0:
            return 0.0
        return self.cost_cny / self.cost_limit_cny

    def check(self) -> str:
        """返回状态："" | "warn" | "exceeded"。warn 每轮只置位一次。"""
        pct = self.percent()
        if self.cost_limit_cny > 0 and pct >= 1.0:
            self.exceeded = True
            return "exceeded"
        if self.cost_limit_cny > 0 and pct >= self.warn_pct and not self.warned:
            self.warned = True
            return "warn"
        return ""


def tokens_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """按 env 单价估算一次调用的成本（CNY），供 EventSink 记账。"""
    inp, out = _pricing()
    return max(0, prompt_tokens) / 1000.0 * inp + max(0, completion_tokens) / 1000.0 * out
