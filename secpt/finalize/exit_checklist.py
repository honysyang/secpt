"""出场清单三验（WF6）：done 先过验再放行，验不过弹回继续走步。

三条核对（开发文档 WF6 原文）：
1. 账本交齐：有小票无对应 step = 账目缺口（孤儿小票）；
2. findings 全部有终态：无 claimed 悬置（M1 裁判未实现，claimed 必须先收口）；
3. 进场三问齐全：goal/objective/success_criteria 三样从账本还原不缺项。

为什么核对规则落在最外层（finalize）而非 core：分层红线单向（core → llms →
tools/store），core/loop.py 只约定 ExitVerdict 形状与 ExitChecker 协议，
本模块实现之，由 main.py / 测试装配方注入。
"""

from __future__ import annotations

import json
import sqlite3

from secpt.core.loop import ExitVerdict
from secpt.store import ledger


def run_checklist(conn: sqlite3.Connection, task_id: str) -> ExitVerdict:
    """按 WF6 三条逐条核对账本，failures 逐条列出（LLM 弹回后要看得懂堵在哪）。"""
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

    return ExitVerdict(passed=not failures, failures=failures)


class ExitChecklist:
    """ExitChecker 协议的实现体（W2-1 M1 版：三条核对，不做 criteria 命中判定）。"""

    def check(self, conn: sqlite3.Connection, task_id: str) -> ExitVerdict:
        """委托纯函数 run_checklist（同类无状态，纯函数便于单测直调）。"""
        return run_checklist(conn, task_id)
