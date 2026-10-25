"""分层预算（BUILD_SPEC §6.7）：mission 级 token / 步数 / 时间。

每个超级步结算一次 budget.charged（apply_event 递增 state.step，重放
一致）；80% 写 budget.warning——「只写一次」直接由账本 idem_key 去重保证，
不需要扫账本；100% 由 loop 的 check_termination 收口，不在这里抛。
"""
from __future__ import annotations

import time

from pa.models import MissionState
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

WARN_RATIO = 0.8


def charge(state: MissionState, writer: StoreWriter, llm, cursor: list,
           started: float) -> None:
    """结算一个超级步：tokens 按本步 LLM 调用的 prompt 估算（纯函数），
    seconds 按墙钟。cursor[0] 是 llm.calls 的消费位。"""
    calls = getattr(llm, "calls", [])
    fresh = calls[cursor[0]:]
    tokens = sum(llm.estimate_tokens(c["system"] + c["user"]) for c in fresh)
    cursor[0] = len(calls)
    # idem 用 step+1 序号而非当前 step：resume 重放后 step 已是已结算值，
    # 用当前值会撞账本上同序号旧事件（重放即重结算）；序号跨中断跑法确定
    ev = writer.append(
        "budget.charged", "engine",
        {"tokens": tokens, "seconds": max(0, int(time.monotonic() - started))},
        f"bc:{state.mission_id}:{state.step + 1}")
    apply_event(state, ev)
    budget = state.spec.budget
    if budget.tokens and state.spent_tokens >= budget.tokens * WARN_RATIO:
        # 幂等键保证整个 mission 至多一条 budget.warning
        writer.append("budget.warning", "engine",
                      {"spent_tokens": state.spent_tokens,
                       "budget_tokens": budget.tokens},
                      f"bw:{state.mission_id}")
