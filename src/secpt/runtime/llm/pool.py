"""LLM 池：多 provider 故障转移链（C2）。

规则（对齐 ARTEX 的 failover 纪律，适配非流式调用）：
- 池顺序 = SECPT_MODEL_POOL（逗号分隔），如 "deepseek-chat,openai/gpt-4o"
- 单请求失败（可重试异常耗尽后）→ 链上下一个模型；全链失败才抛
- 熔断：某模型连续失败 3 次进入 open（冷却 60s）；冷却到期进入 half-open——
  只放行**一个**探针请求：成功恢复全量（closed）、失败重新熔断（open）。
  旧实现冷却到期即全量恢复，上游仍在故障时会再吃满 3 次失败才重新熔断，
  形成 60s 周期的失败脉冲；半开探测把这个脉冲收敛为单请求代价。
- 探针位带时间戳（_PROBE_TIMEOUT_S）：探针被取消/挂死时不会永久占位。
- 熔断注册表是进程内状态（持久化后续再加）；与 SECPT_MODEL_BASE_<PREFIX>
  生态正交（池里每个名字仍走 resolve_model 路由）。
"""

from __future__ import annotations

import asyncio
import os
import time

from secpt.runtime.llm.client import call_llm

_BREAK_THRESHOLD = 3
_COOLDOWN_S = 60.0
_PROBE_TIMEOUT_S = 120.0  # 探针在途占位上限（超时视为挂死，可再放探针）

# model → [连续失败数, 熔断至(until_ts), 探针发起时间(0=无)]
_circuits: dict[str, list[float]] = {}


def pool_models() -> list[str]:
    raw = os.environ.get("SECPT_MODEL_POOL", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def _broken(model: str) -> bool:
    """只读判断：当前是否处于 open 冷却期（不改变状态；观测/测试用）。"""
    st = _circuits.get(model)
    if not st:
        return False
    fails, until, _probe = st
    return fails >= _BREAK_THRESHOLD and time.time() < until


def _admit(model: str) -> bool:
    """是否放行本次调用。

    closed（未熔断）→ True；open 冷却中 → False；冷却到期 → 放行 1 个探针
    （探针在途时其余请求继续拒绝，等探针结论）。
    """
    st = _circuits.get(model)
    if st is None:
        return True
    fails, until, probe = st
    if fails < _BREAK_THRESHOLD:
        return True
    now = time.time()
    if now < until:
        return False
    if probe and (now - probe) < _PROBE_TIMEOUT_S:
        return False  # 探针在途（或挂死未超时）：不放行第二个
    st[2] = now  # 占用探针位
    return True


def _record_fail(model: str) -> None:
    st = _circuits.setdefault(model, [0.0, 0.0, 0.0])
    st[0] += 1
    st[2] = 0.0  # 归还探针位（探针失败 → 立即回到 open 全量熔断）
    if st[0] >= _BREAK_THRESHOLD:
        st[1] = time.time() + _COOLDOWN_S


def _record_ok(model: str) -> None:
    _circuits.pop(model, None)  # 探针成功 → closed 全量恢复


async def call_pooled(
    messages: list,
    tools: list,
    model: str,
    api_key: str | None = None,
    fallback_model: str | None = None,
) -> object:
    """池化调用。无池配置时退化为单模型调用（行为与 call_llm 完全一致）。

    请求模型优先：mentor/verify/escalate 点名的模型不被池覆盖（PA2 语义——
    "导师用 deepseek-v4-pro"不能被池首项悄悄换成别的），池成员作为出现意外
    （可重试失败耗尽/熔断）时的自动切换后备；去重保序。
    """
    members = pool_models()
    chain = [model] + [m for m in members if m != model] if members else [model]
    last_exc: Exception | None = None
    for m in chain:
        if not _admit(m):
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
