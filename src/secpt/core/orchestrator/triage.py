"""Orchestration triage: the fast "JEV" judgment layer for tasks and results.

Two jobs, both deliberately cheap and deterministic by default:

1. **Task triage** — when the planner (or a worker) proposes a task, decide:
   - lane: main path or background logistics;
   - urgency/priority hint;
   - whether the result needs delivery review.
2. **Result quick-check** — before a task is reported as an answer, do a
   cheap sanity check (real flag shape, empty answer, goal requirement).

The external TypeSafe JEV gate (``secpt.evaluation.jev``) is optional.  Rules
are always available and are the fallback when the gate is unavailable, so the
orchestrator never depends on a network call for correctness.  Set
``SECPT_ORCH_JEV=1`` and ``JEV_API_KEY`` to let the typed gate refine the
background-work decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from secpt.core.orchestrator.policy import prove_goal

# Work that is valuable but not on the critical path.  Keep the patterns
# deliberately narrow: a task that merely contains "scan" is not automatically
# background work (a targeted scan may be the main path).
_LOGISTICS_PATTERNS = (
    r"\bdownload(?:s|ing)?\b",
    r"\bwget\b",
    r"curl\s+[^|;]*\s-o\b",
    r"\bgit\s+clone\b",
    r"\b(?:apt|apt-get|yum|dnf|apk|pip|pip3|npm|go)\s+install\b",
    r"\bwordlist(?:s)?\b",
    r"\bdictionar(?:y|ies)\b",
    r"\bbrute[- ]?forc(?:e|ing)\b",
    r"\b(?:mass|large[- ]scale|bulk)\s+(?:scan|search|download|collect)",
    r"\b(?:full|whole)[- ](?:port|subdomain|directory)\s+(?:scan|enum)",
    r"\b(?:gobuster|feroxbuster|ffuf|dirb|dirsearch|wfuzz|sqlmap\s+--crawl)\b",
    r"\b(?:crawl|spider)\b",
    r"(?:下载|安装|批量|大量|全量|字典|爆破|爬取|收集资料|搜集)",
)

_VERIFY_PATTERNS = (
    r"\bflag\b",
    r"\bconfirmed?\b",
    r"\bverif(?:y|ication)\b",
    r"\breproduc(?:e|tion)\b",
    r"\bexploit\b",
    r"(?:复验|复现|验证|确认|拿到|证明)",
)

_RISK_PATTERNS = (
    r"\brm\s+-rf\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bddos\b",
    r"\bwipe\b",
    r"(?:删除|清空|关机|破坏)",
)


@dataclass(frozen=True)
class TaskTriage:
    """Result of a fast task judgment."""

    lane: str = "main"  # main | logistics
    priority: int | None = None
    needs_delivery: bool = False
    risk: str = "normal"  # normal | high
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "lane": self.lane,
            "priority": self.priority,
            "needs_delivery": self.needs_delivery,
            "risk": self.risk,
            "reason": self.reason,
        }


def _matches(patterns: tuple[str, ...], text: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def triage_task(
    title: str,
    goal: str = "",
    done_criteria: str = "",
    *,
    priority: int | None = None,
    role: str = "",
    asset_scope: str = "",
) -> TaskTriage:
    """Deterministic fast judgment for a proposed task.

    Logistics is chosen only for work that is both mechanically long-running
    and not itself a verification/exploitation deliverable.  A "verify the
    flag" task is main-path work even if it uses a wordlist.
    """
    text = " ".join([title or "", goal or "", done_criteria or "", asset_scope or ""])
    needs_delivery = bool(_matches(_VERIFY_PATTERNS, text)) or role in {"verify", "report"}
    risk_hit = _matches(_RISK_PATTERNS, text)
    logistics_hit = _matches(_LOGISTICS_PATTERNS, text)
    is_logistics = bool(logistics_hit) and not needs_delivery and role not in {"verify", "report"}
    lane = "logistics" if is_logistics else "main"
    p = priority
    if p is None:
        p = 3 if is_logistics else 5
    reason = ""
    if is_logistics:
        reason = f"耗时/重复工作（命中 {logistics_hit!r}）→ 后勤后台"
    elif needs_delivery:
        reason = "结果需要交付核验"
    if risk_hit:
        reason = (reason + "；" if reason else "") + f"高风险动作（命中 {risk_hit!r}）"
    return TaskTriage(
        lane=lane,
        priority=max(1, min(10, int(p))),
        needs_delivery=needs_delivery,
        risk="high" if risk_hit else "normal",
        reason=reason,
    )


async def refine_with_jev(
    triage: TaskTriage,
    *,
    title: str,
    goal: str = "",
    gate=None,
) -> TaskTriage:
    """Optionally let the external typed JEV gate refine the lane decision.

    The gate is advisory: a low-confidence or unavailable gate leaves the
    deterministic result unchanged.  It is off by default so tests and offline
    runs stay deterministic.
    """
    import os

    if os.environ.get("SECPT_ORCH_JEV", "0") != "1":
        return triage
    try:
        if gate is None:
            from secpt.evaluation.jev import JevGate

            gate = JevGate()
        if not gate.available():
            return triage
        decision = await gate.decide(
            "这个任务是否是耗时、重复、可异步执行的后勤工作（下载/批量搜索/爆破/爬取），"
            "而不是主任务必须等待的步骤？",
            f"title={title}\ngoal={goal}\nrule_lane={triage.lane}\nrule_reason={triage.reason}",
        )
        if not gate.accept(decision):
            return triage
        if decision.answer and triage.lane != "logistics":
            return replace(
                triage,
                lane="logistics",
                priority=min(triage.priority or 5, 3),
                reason=(triage.reason + "；" if triage.reason else "") + "JEV 判定为后勤",
            )
        if not decision.answer and triage.lane == "logistics":
            return replace(
                triage,
                lane="main",
                priority=max(triage.priority or 5, 5),
                reason=(triage.reason + "；" if triage.reason else "") + "JEV 判定为主路径",
            )
        return triage
    except Exception:
        return triage


def quick_check_result(answer: str, done_criteria: str = "") -> tuple[bool, str]:
    """Cheap pre-delivery sanity check (no LLM).

    Returns ``(ok, reason)``.  A flag requirement is enforced only when the
    criterion contains "flag" without an "或" alternative, matching ``prove_goal``
    semantics used by the frontier.
    """
    text = (answer or "").strip()
    if not text:
        return False, "空结果"
    criterion = (done_criteria or "").strip()
    if "flag" in criterion.lower() and "或" not in criterion:
        ok, why = prove_goal(criterion, text)
        if not ok:
            return False, why
    return True, "结果形态通过快检"
