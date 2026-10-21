"""Coach: deterministic stuck-point intervention for the orchestrator.

The engine already has a stuck detector and optional mentor/LLM advice.  This
module adds a *coach* at the orchestration layer: before a failed intent is
retried (or RESET into a fresh session), derive an actionable note from the
failure history and inject it into the retry objective.

Goals:
- never repeat the same failed route;
- shrink an over-budget task instead of burning another full budget;
- repair tool/environment preconditions before retrying;
- keep the intervention short enough to fit in a prompt.

No LLM call is made here; the note is deterministic.  The engine's mentor
remains the optional model-backed deeper diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass

from secpt.runtime.stuck import FLAG_RE


@dataclass(frozen=True)
class CoachAdvice:
    kind: str  # retry | reset | shrink | repair | stop
    note: str


def _recent_failures(recent: list[dict] | None, limit: int = 3) -> list[str]:
    out: list[str] = []
    for row in (recent or [])[:limit]:
        if str(row.get("status") or "") not in ("exhausted", "blocked"):
            continue
        reason = str(row.get("fail_reason") or "").strip()
        if reason:
            out.append(reason[:160])
    return out


def coach_advice(
    title: str,
    *,
    status: str = "",
    fail_reason: str = "",
    attempts: int = 1,
    max_steps: int = 0,
    recent: list[dict] | None = None,
    done_criteria: str = "",
) -> CoachAdvice:
    """Derive a concise coaching note for a failed/retried intent."""
    why = f"{status} {fail_reason}".lower()
    fails = _recent_failures(recent)
    repeated = len([x for x in fails if x and x in (fail_reason or "")]) >= 2

    if "stalled" in why or "无进展" in why or "卡死" in why:
        kind = "reset"
        action = (
            "停止继续同一路线。换一个证据源/工具或入口重试；先用一条最小命令验证新假设，"
            "确认有效后再扩展，不要重复上轮动作。"
        )
    elif "step_exceeded" in why or "wallclock" in why or "预算" in why:
        kind = "shrink"
        action = (
            "预算已经打满：把任务拆成一个最小可验证步骤（一个请求、一个文件、一个端点），"
            "先拿到中间结论；不要再次铺开全量枚举。"
        )
    elif "error" in why or "timeout" in why or "connection" in why:
        kind = "repair"
        action = (
            "先修复前置条件：确认目标可达、依赖/工具存在、权限与代理设置正确；"
            "用一条健康检查命令验证后再重试，不要盲目重跑。"
        )
    elif attempts >= 2 or repeated:
        kind = "reset"
        action = (
            "同一路线已连续失败。列出已被否定的路线，禁止重复；改换攻击面/工具/编码方式，"
            "并在本步结束时给出明确的成功或否定结论。"
        )
    else:
        kind = "retry"
        action = (
            "重试前先说明上轮为什么没完成，并选择一个与上轮不同的动作；"
            "如果信息不足，先补最小侦察再执行。"
        )

    lines = [
        "【教练介入】",
        f"- 任务：{title[:120]}",
        f"- 观察：{why.strip() or '上一轮未达成完成判据'}",
    ]
    if done_criteria:
        lines.append(f"- 完成判据：{done_criteria[:200]}")
    if fails:
        lines.append("- 近期否定：" + "；".join(fails[:2]))
    lines.append(f"- 下一步：{action}")
    return CoachAdvice(kind=kind, note="\n".join(lines))


def coach_for_retry(intent, recent: list[dict] | None = None) -> str:
    """Convenience wrapper for a Frontier Intent (duck-typed)."""
    advice = coach_advice(
        str(getattr(intent, "title", "") or ""),
        status="retry",
        fail_reason=str(getattr(intent, "fail_reason", "") or ""),
        attempts=int(getattr(intent, "attempts", 1) or 1),
        max_steps=int(getattr(intent, "max_steps", 0) or 0),
        recent=recent,
        done_criteria=str(getattr(intent, "done_criteria", "") or ""),
    )
    return advice.note


def has_flag(text: str) -> bool:
    """Small helper for delivery/coach callers."""
    return bool(FLAG_RE.search(text or ""))
