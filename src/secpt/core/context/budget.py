"""S3 预算追踪 BudgetTracker。

累计 prompt_tokens / completion_tokens / cost_cny / wall_s。
单价解析（pricing_for，按模型）优先级：
SECPT_PRICE_<PREFIX>_IN_PER_1K / _OUT_PER_1K（provider 级 env 覆盖） >
内置模型名表 > 内置 provider 表 > SECPT_PRICING_INPUT_PER_1K/OUT（通用 env，
默认 0.002/0.008 CNY per 1K，历史 deepseek 价）。

阈值行为：
- 达到 warn_pct（默认 80%）→ check() 返回 "warn"，由 Engine 往 messages
  追加一条 user 引导消息（只触发一次）
- 达到 100% → check() 返回 "exceeded"，由 Engine 优雅收尾：
  先落 findings + 阶段性 report.md，status=budget_exceeded，不抛异常
"""

from __future__ import annotations

import os
import time

# 内置计价表（CNY / 1K tokens；参考 2025-2026 官网档位，随官方调整——env 可覆盖）
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.002, 0.008),
    "deepseek-reasoner": (0.004, 0.016),
    "gpt-4o": (0.018, 0.072),
    "gpt-4o-mini": (0.0011, 0.0044),
}
_PROVIDER_PRICES: dict[str, tuple[float, float]] = {
    "DEEPSEEK": (0.002, 0.008),
    "OPENAI": (0.018, 0.072),
}


def _pricing() -> tuple[float, float]:
    """通用 token 单价（CNY/1K，兼容旧配置）：env 可覆盖，默认 deepseek-chat 价。"""
    inp = float(os.environ.get("SECPT_PRICING_INPUT_PER_1K", "0.002"))
    out = float(os.environ.get("SECPT_PRICING_OUTPUT_PER_1K", "0.008"))
    return inp, out


def _provider_prefix(model: str) -> str:
    """模型名 → provider 前缀（大写，与 SECPT_MODEL_*_<PREFIX> 生态一致）。"""
    if "/" in model:
        return model.partition("/")[0].upper().replace("-", "_")
    return "DEEPSEEK"  # 裸名默认 provider（与 llm/client.resolve_model 口径一致）


def pricing_for(model: str = "") -> tuple[float, float]:
    """按模型解析单价（CNY/1K tokens）。

    provider 级 env（SECPT_PRICE_<PREFIX>_IN/OUT_PER_1K）> 精确模型名表 >
    provider 表 > 通用 env/默认价。model 为空时等价 _pricing()（向后兼容）。
    """
    fallback = _pricing()
    prefix = _provider_prefix(model) if model else ""
    if prefix:
        env_in = os.environ.get(f"SECPT_PRICE_{prefix}_IN_PER_1K")
        env_out = os.environ.get(f"SECPT_PRICE_{prefix}_OUT_PER_1K")
        if env_in not in (None, "") or env_out not in (None, ""):
            return (
                float(env_in) if env_in not in (None, "") else fallback[0],
                float(env_out) if env_out not in (None, "") else fallback[1],
            )
    if model:
        mid = model.partition("/")[2] or model
        if mid in _MODEL_PRICES:
            return _MODEL_PRICES[mid]
        if prefix in _PROVIDER_PRICES:
            return _PROVIDER_PRICES[prefix]
    return fallback


WARN_80_MSG = "预算已用 80%。请停止探索，把你已确认的事实整理成结构化结论输出。"


class BudgetTracker:
    def __init__(
        self,
        cost_limit_cny: float = 0.0,
        *,
        warn_pct: float = 0.8,
        input_per_1k: float | None = None,
        output_per_1k: float | None = None,
        model: str = "",
    ) -> None:
        _inp, _out = pricing_for(model)
        self.input_per_1k = _inp if input_per_1k is None else input_per_1k
        self.output_per_1k = _out if output_per_1k is None else output_per_1k
        self.model = model  # 计价依据（记录用；failover 段按主模型价近似）
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


def tokens_cost(prompt_tokens: int, completion_tokens: int, model: str = "") -> float:
    """按模型单价估算一次调用的成本（CNY），供 EventSink 记账。"""
    inp, out = pricing_for(model)
    return max(0, prompt_tokens) / 1000.0 * inp + max(0, completion_tokens) / 1000.0 * out
