"""S3 上下文压缩 Compaction。

触发：est_messages_tokens >= 0.72 * window（0.72：实测 0.55 时压缩过频——
     68 步触发 21 次、每次仅删 4 条，摘要 LLM 调用浪费；提高阈值后每次压缩
     批量删到 0.5×window 以下，次数大幅下降。append-only 命中 prefix cache
     更便宜，压缩会打破命中，故不追求更低阈值）。

保护：
- 首条 system 消息
- 最新 12 条消息（PROTECT_LATEST）
- 已记录 finding/credential（存在 store/events，天然不在 messages 里被删，
  且摘要 prompt 会要求模型把凭据写进摘要）

压缩对象：最老的一批，**必须按整组删除**
    (assistant[含 tool_calls] + 它的全部 tool messages) 是一组
    ⚠️ 只删 assistant 不删 tool result（或反过来）都会导致 API 400。
产物：在被删区间原位插入 {"role":"user","content":"【历史摘要 #k】\n<六段摘要>"}

红线：本模块（Compaction）是除 Rollover 之外唯一允许删改 messages 的地方。
"""

from __future__ import annotations

from secpt.core.context.estimator import est_messages_tokens

COMPACT_THRESHOLD = 0.72
COMPACT_TARGET = 0.5  # 批量删除目标：一次压到 0.5×window 以下（少次多删）
PROTECT_LATEST = 6  # 尾部保护条数（12 时大输出场景保护尾本身超阈值，压缩永远压不下去）
MAX_GROUPS_PER_COMPACT = 200  # 单次压缩最多删组数（安全上限）


def should_compact(messages: list[dict], window: int) -> bool:
    """是否触发压缩。"""
    if window <= 0:
        return False
    return est_messages_tokens(messages) >= COMPACT_THRESHOLD * window


def _call_ids(msg: dict) -> set[str]:
    return {tc.get("id") for tc in (msg.get("tool_calls") or [])}


def group_end(messages: list[dict], start: int) -> int:
    """返回 messages[start] 所在组的结束下标（含）。

    组 = assistant（含 tool_calls）+ 它的全部 tool messages（连续）。
    无 tool_calls 的 assistant 组就是它自己。
    """
    needed = _call_ids(messages[start])
    if not needed:
        return start
    j = start
    while j + 1 < len(messages):
        nxt = messages[j + 1]
        if nxt.get("role") == "tool" and nxt.get("tool_call_id") in needed:
            j += 1
            needed.discard(nxt.get("tool_call_id"))
            if not needed:
                break
        else:
            break
    return j


def _is_complete_group(messages: list[dict], start: int) -> bool:
    """该组是否完整（assistant 的全部 tool_call 都有对应 tool 结果）。"""
    needed = _call_ids(messages[start])
    if not needed:
        return True
    have = 0
    for x in messages[start + 1 :]:
        if x.get("role") == "tool" and x.get("tool_call_id") in needed:
            have += 1
    return have >= len(needed)


def _protected_start(messages: list[dict]) -> int:
    """可删区间的起始下标：首条 system 之后。"""
    lo = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            lo = i + 1
            break
    return lo


def _next_removable(messages: list[dict], lo: int, protected_tail: int) -> tuple[int, int] | None:
    """在 [lo, len-protected_tail) 内找第一个可删整组，返回 (start, end)；无则 None。"""
    end_limit = max(lo, len(messages) - protected_tail)
    i = lo
    while i < end_limit:
        if messages[i].get("role") == "assistant" and _is_complete_group(messages, i):
            return i, group_end(messages, i)
        i += 1
    return None


async def compact_messages(
    messages: list[dict],
    objective: str,
    summarizer,
    window: int,
    *,
    k: int = 0,
    protected_tail: int = PROTECT_LATEST,
):
    """压缩一次：删最老整组直到 token 回落，原位插一条六段摘要。

    返回 (新 messages, 删除条数, 摘要文本)。removed==0 表示未发生压缩。
    """
    if not should_compact(messages, window):
        return messages, 0, ""

    remaining = list(messages)
    lo = _protected_start(remaining)
    removed_msgs: list[dict] = []
    first_idx: int | None = None

    # 批量删除：目标 0.5×window（一次压到位，避免每步触发压缩）
    target = COMPACT_TARGET * window
    while est_messages_tokens(remaining) >= target:
        hit = _next_removable(remaining, lo, protected_tail)
        if hit is None:
            break
        idx, end = hit
        if first_idx is None:
            first_idx = idx
        removed_msgs += remaining[idx : end + 1]
        del remaining[idx : end + 1]
        # lo 若落在被删区间之后，回退到被删位置继续向前找
        if lo > idx:
            lo = idx
        if len(removed_msgs) > MAX_GROUPS_PER_COMPACT:
            break

    if not removed_msgs:
        return messages, 0, ""

    summary = await summarizer(objective, removed_msgs)
    summary_msg = {"role": "user", "content": f"【历史摘要 #{k}】\n{summary}"}
    insert_at = min(first_idx if first_idx is not None else 0, len(remaining))
    remaining.insert(insert_at, summary_msg)
    return remaining, len(removed_msgs), summary
