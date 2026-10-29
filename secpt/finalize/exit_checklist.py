"""出场清单五验（WF6 + M5 交付/规划）：done 先过验再放行，验不过弹回继续走步。

五条核对：
1. 账本交齐：有小票无对应 step = 账目缺口（孤儿小票）；
2. findings 全部有终态：无 claimed 悬置；
3. 进场三问齐全：goal/objective/success_criteria 三样从账本还原不缺项；
4. **交付对目标（M5，设计文档 L24-28）**：profile 声明的验收条目（``[[acceptance]]``）
   逐条被 verified finding 命中——"完成≠可出场"，只判 claimed 不算交付。
5. **规划全部收口（M5，设计文档 L16）**：plans 表无 active 行——"每一条规划是否
   都完成"要能机器判定，由主 LLM 用 ``plan_done``/``plan_cancel`` 收口。

为什么第 4 条由 profile 驱动而不是猜 criteria 文本：criteria 是自然语言
（"拿到 flag"），机器判据必须是可一键复算的清单；没配验收条目 = 不判（如实
不虚红：mock 演示/探索型任务本来就没有硬性交付物）。判据口径与 R4 一致：
title+detail 子串、不分大小写，只是把来源限定在 verified 行。

为什么第 5 条只在有规划时生效：没提过规划的任务（mock 演示/短任务）没有
"规划完成"可言；提过就必须收口，否则"规划 A/B/C"只是嘴上一说。

为什么核对规则落在最外层（finalize）而非 core：分层红线单向，core/loop.py
只约定 ExitVerdict 形状与 ExitChecker 协议，本模块实现之，由 main.py / 测试
装配方注入。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from pydantic import BaseModel

from secpt.core.loop import ExitVerdict
from secpt.store import ledger


class AcceptanceRule(BaseModel):
    """验收条目：id 只用于报错点名，pattern 是 verified finding 的子串判据。"""

    id: str
    pattern: str


def run_checklist(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    acceptance: Sequence[AcceptanceRule] = (),
) -> ExitVerdict:
    """按五条逐条核对账本，failures 逐条列出（LLM 弹回后要看得懂堵在哪）。"""
    failures: list[str] = []

    # 条 1：孤儿小票——tickets 有 step_id 但 steps 无对应行 = 账本交不齐。
    orphans = conn.execute(
        "SELECT t.summary FROM tickets t"
        " LEFT JOIN steps s ON s.id = t.step_id"
        " WHERE t.task_id = ? AND s.id IS NULL",
        (task_id,),
    ).fetchall()
    if orphans:
        failures.append(f"账本交不齐：{len(orphans)} 张孤儿小票（如「{orphans[0]['summary']}」）")

    # 条 2：claimed 悬置=0——所有 finding 必须落 verified/rejected 终态。
    pending = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE task_id = ? AND verify_state = 'claimed'",
        (task_id,),
    ).fetchone()[0]
    if pending:
        failures.append(f"claimed 悬置 {pending} 条未收口（需 verified/rejected 终态）")

    # 条 3：进场三问齐全——goal + objective/success_criteria 从账本还原。
    task = ledger.get_task(conn, task_id)
    missing: list[str] = []
    if not (task.goal or "").strip():
        missing.append("goal")
    config: dict[str, object] = {}
    try:
        config = json.loads(ledger.latest_run(conn, task_id).config_json)
    except json.JSONDecodeError:
        pass
    for key in ("objective", "success_criteria"):
        if not str(config.get(key) or "").strip():
            missing.append(key)
    if missing:
        failures.append(f"进场三问缺失：{'、'.join(missing)}（criteria 原文应如实转录）")

    # 条 4：交付对目标（M5）——验收条目逐条须被 verified finding 命中。
    if acceptance:
        haystack = "\n".join(
            f"{row.title}\n{row.detail}"
            for row in ledger.task_findings(conn, task_id, verify_state="verified")
        ).lower()
        missed = [rule.id for rule in acceptance if rule.pattern.lower() not in haystack]
        if missed:
            failures.append(
                f"交付对目标：验收条目未命中 {missed}（需 verified finding 命中；claimed 不算交付）"
            )

    # 条 5：规划全部收口（M5）——plans 无 active 行。
    open_plans = conn.execute(
        "SELECT COUNT(*) FROM plans WHERE task_id = ? AND status = 'active'", (task_id,)
    ).fetchone()[0]
    if open_plans:
        failures.append(
            f"规划未收口 {open_plans} 条（active）——用 plan_done 标记完成 / plan_cancel 标记废弃"
        )

    return ExitVerdict(passed=not failures, failures=failures)


class ExitChecklist:
    """ExitChecker 协议的实现体（M1 三条 + M5 交付验收 + 规划收口）。"""

    def __init__(self, acceptance: Sequence[AcceptanceRule] = ()) -> None:
        """acceptance 由装配层从 profile 映射（finalize 不 import judge：分层单向）。"""
        self._acceptance = tuple(acceptance)

    def check(self, conn: sqlite3.Connection, task_id: str) -> ExitVerdict:
        """委托纯函数 run_checklist（同类无状态，纯函数便于单测直调）。"""
        return run_checklist(conn, task_id, acceptance=self._acceptance)


__all__ = ["AcceptanceRule", "ExitChecklist", "run_checklist"]
