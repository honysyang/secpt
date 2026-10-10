"""LLM 池：多 provider 故障转移链（C2）。

规则（对齐 ARTEX 的 failover 纪律，适配非流式调用）：
- 池顺序 = SECPT_MODEL_POOL（逗号分隔），如 "deepseek-chat,openai/gpt-4o"
- 单请求失败（可重试异常耗尽后）→ 链上下一个模型；全链失败才抛
- 熔断：某模型连续失败 3 次冷却 60s（进程内注册表；持久化后续再加）
- 与 SECPT_MODEL_BASE_<PREFIX> 生态正交（池里每个名字仍走 resolve_model 路由）
"""

from __future__ import annotations

import asyncio
import os
import time

from secpt.runtime.llm.client import call_llm

_BREAK_THRESHOLD = 3
_COOLDOWN_S = 60.0
_circuits: dict[str, tuple[int, float]] = {}  # model → (连续失败数, 熔断至)


def pool_models() -> list[str]:
    raw = os.environ.get("SECPT_MODEL_POOL", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def _broken(model: str) -> bool:
    fails, until = _circuits.get(model, (0, 0.0))
    return fails >= _BREAK_THRESHOLD and time.time() < until


def _record_fail(model: str) -> None:
    fails, _ = _circuits.get(model, (0, 0.0))
    fails += 1
    _circuits[model] = (fails, time.time() + _COOLDOWN_S if fails >= _BREAK_THRESHOLD else 0.0)


def _record_ok(model: str) -> None:
    _circuits.pop(model, None)


async def call_pooled(
    messages: list,
    tools: list,
    model: str,
    api_key: str | None = None,
    fallback_model: str | None = None,
) -> object:
    """池化调用。无池配置时退化为单模型调用（行为与 call_llm 完全一致）。"""
    pool = pool_models() or [model]
    last_exc: Exception | None = None
    for m in pool:
        if _broken(m):
            continue
        try:
            resp = await call_llm(messages, tools, m, api_key, fallback_model)
            _record_ok(m)
            return resp
        except Exception as e:
            _record_fail(m)
            last_exc = e
            await asyncio.sleep(0.5)
    assert last_exc is not None
    raise last_exc
