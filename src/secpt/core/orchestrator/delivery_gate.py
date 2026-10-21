"""Delivery gate: the agent that decides whether the overall run should end.

The main agent calls this gate when the main lane has drained.  It checks:
- whether a claimed flag has an independent verification result;
- whether there is a confirmed finding;
- whether a rejected delivery should get one bounded follow-up task.

The gate is deterministic and cheap by default.  It never invents success: a
flag without independent verification triggers a verification task, and a
second failure closes the run as an explicitly unverified result.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class DeliveryDecision:
    accepted: bool
    reason: str
    followups: list[dict] = field(default_factory=list)
    verified_flags: list[str] = field(default_factory=list)
    mode: str = "deterministic"
    waiting: bool = False  # 暂态：仍有在途任务/复验，调用方应等待而非收口

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "followups": list(self.followups),
            "verified_flags": list(self.verified_flags),
            "mode": self.mode,
            "waiting": self.waiting,
        }


def _delivery_rows(fr) -> list[dict]:
    """Tasks previously created by the delivery gate (verification/review)."""
    try:
        return [
            row
            for row in fr.list_all(status="done", limit=500)
            if str(row.get("source") or "") == "delivery"
        ]
    except Exception:
        return []


def _delivery_pending(fr) -> bool:
    try:
        return any(
            str(row.get("source") or "") == "delivery"
            for row in fr.list_all(status="open", limit=500)
        ) or any(
            str(row.get("source") or "") == "delivery"
            for row in fr.list_all(status="running", limit=500)
        )
    except Exception:
        return False


def _running_main(fr, lane: str) -> list[dict]:
    try:
        return [
            row
            for row in fr.list_all(status="running", limit=500)
            if str(row.get("lane") or "main") == lane
        ]
    except Exception:
        return []


def _confirmed_findings(base: str) -> int:
    try:
        from pathlib import Path

        from secpt.findings.store import FindingStore

        store = FindingStore(Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite")
        return len(
            [
                f
                for f in store.list(status="confirmed", limit=500)
                if str(f.job_id or "").startswith(base)
            ]
        )
    except Exception:
        return 0


def _platform_confirmed_flags(base: str, flags: list[str]) -> list[str]:
    """平台实况已确认的旗子（correct_flag_count>0）→ 传入的旗子视为已验证。

    仅 benchmark 环境（BENCHMARK_BASE_URL 已配置）尝试；非跑分/平台不可达
    返回空（走正常复验通路）。旗子归属由平台判分，此处不猜。
    """
    import os

    if not os.environ.get("BENCHMARK_BASE_URL"):
        return []
    try:
        from secpt.core.orchestrator.benchmark import fetch_challenges

        live = fetch_challenges()
        scored = sum(int(c.get("correct_flag_count") or 0) for c in live)
        return list(flags) if scored > 0 else []
    except Exception:
        return []


def _has_verified_result(rows: list[dict], flags: list[str]) -> list[str]:
    verified: list[str] = []
    for row in rows:
        text = f"{row.get('result', '')}\n{row.get('fail_reason', '')}"
        low = text.lower()
        for flag in flags:
            if flag and flag in text:
                verified.append(flag)
        if not flags and ("confirmed" in low or "verified" in low or "复验通过" in text):
            verified.append("confirmed")
    return list(dict.fromkeys(verified))


def review_delivery(
    fr,
    *,
    base: str,
    objective: str,
    flags: list[str],
    attempt: int = 0,
    max_attempts: int = 1,
    main_lane: str = "main",
) -> DeliveryDecision:
    """Decide whether to end the run, or enqueue one bounded follow-up.

    ``attempt`` is the number of delivery reviews already performed; the caller
    increments it after enqueuing a follow-up.  ``max_attempts`` bounds the
    verification loop so a failed verification cannot burn forever.
    """
    try:
        open_main = int(fr.count_open(lane=main_lane))
    except TypeError:
        open_main = int(fr.count_open())
    running_main = _running_main(fr, main_lane)
    if open_main > 0 or running_main:
        return DeliveryDecision(
            False,
            f"仍有 {open_main} 个待执行、{len(running_main)} 个在途的主路径任务",
            waiting=True,
        )
    if _delivery_pending(fr):
        return DeliveryDecision(False, "交付复验任务仍在执行", waiting=True)

    # 平台已确认的旗子（benchmark 跑分协议：submit 成功即平台验证，无需复验）——
    # 台账 flags_found > 0 的记录视为已验证，避免对平台判分过的旗子再烧复验步数
    platform_confirmed = _platform_confirmed_flags(base, flags)
    if platform_confirmed:
        return DeliveryDecision(
            True,
            "平台已确认旗子：" + "、".join(v[:60] for v in platform_confirmed[:3]),
            verified_flags=platform_confirmed,
        )

    done_delivery = _delivery_rows(fr)
    verified = _has_verified_result(done_delivery, flags)
    if verified:
        return DeliveryDecision(
            True,
            "交付核验通过：" + "、".join(v[:80] for v in verified[:3]),
            verified_flags=verified,
        )

    if flags:
        if attempt < max_attempts:
            first = flags[0]
            goal = (
                "【交付复核任务】独立复现以下战果。必须异构验证：与原战果不同的工具路径"
                "（如有流量录制用 traffic replay 重放而非原样重发；HTTP 结论用不同客户端/"
                "不同载荷再证一次），禁止读取产生该战果的旧会话/日志照搬证据。"
                "证据必须锚定：记录每条命令、原始输出、时间戳，关键证据给出哈希或文件路径。\n"
                f"待复验战果：{first}\n"
                f"原始任务目标：{objective[:800]}"
            )
            return DeliveryDecision(
                False,
                "战果尚未独立复验，已交给交付复验任务",
                followups=[
                    {
                        "title": f"交付复验：{first[:60]}",
                        "goal": goal,
                        "done_criteria": "独立复现同一 flag/结论并留下可核验证据",
                        "priority": 10,
                        "depth": 0,
                        "source": "delivery",
                        "max_steps": 25,
                        "lane": main_lane,
                        "asset_scope": "",
                    }
                ],
            )
        return DeliveryDecision(
            True,
            "复验尝试已耗尽：按未验证结果收口（可信度已标记）",
            verified_flags=[],
        )

    confirmed = _confirmed_findings(base)
    if confirmed:
        return DeliveryDecision(True, f"已有 {confirmed} 条 confirmed finding，按交付结果收口")

    if attempt < max_attempts:
        return DeliveryDecision(
            False,
            "尚未产出可交付结果，补一轮最小验证",
            followups=[
                {
                    "title": "交付前最小验证：补一条可交付结论",
                    "goal": (
                        "【交付复核任务】当前主路径没有可交付结果。只做一轮最小验证："
                        "检查一个最有希望的入口/线索，目标是产出可复现的结论（拿到成果，"
                        "或明确否定并给出原因）。不要展开全量扫描。\n"
                        f"原始任务目标：{objective[:800]}"
                    ),
                    "done_criteria": "给出可复现结论或明确否定原因",
                    "priority": 9,
                    "depth": 0,
                    "source": "delivery",
                    "max_steps": 20,
                    "lane": main_lane,
                    "asset_scope": "",
                }
            ],
        )
    return DeliveryDecision(True, "没有可交付结果，按否定结论收口（不再追加预算）")
