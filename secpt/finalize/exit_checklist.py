"""出场清单五验（WF6 + M5 交付/规划）：done 先过验再放行，验不过弹回继续走步。

五条核对：
1. 账本交齐：有小票无对应 step = 账目缺口（孤儿小票）；
2. findings 全部有终态：无 claimed 悬置；
3. 进场三问齐全：goal/objective/success_criteria 三样从账本还原不缺项；
4. **交付对目标（M5，设计文档 L24-28）**：profile 声明的验收条目（``[[acceptance]]``）
   逐条被 verified finding 命中——"完成≠可出场"，只判 claimed 不算交付。
5. **规划全部收口（M5，设计文档 L16）**：plans 表无 active 行——"每一条规划是否
   都完成"要能机器判定，由主 LLM 用 ``plan_done``/``plan_cancel`` 收口。
6. **步骤全部完成（M5-19，设计文档 L21）**：steps 无 voided/aborted 等非 ok 行。

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
        " WHERE t.task_id = ? AND s.id IS NULL"
        " AND t.step_id NOT LIKE 'worker-%'",  # worker 小票无主循环步（M5-25）
        (task_id,),
    ).fetchall()
    if orphans:
        failures.append(f"账本交不齐：{len(orphans)} 张孤儿小票（如「{orphans[0]['summary']}」）")

    # 条 1 补充（M5-15）：小票/双图一致——ok 步要有探索图节点、verified finding
    # 要有证据图节点。
    failures.extend(_graph_gaps(conn, task_id))

    # 条 2：claimed 悬置=0——所有 finding 必须落 verified/rejected 终态。
    # M5-31 文案补"勿重复申报"：真实 run 实测模型为过关反复申报同名 finding
    # （1 条攒成 2 条直到 E_WALL）——失败提示要把"这条路不通"明说，并把
    # 可用的收口路径列出来。sweep_count ≥ 6 = 裁判多轮无法判真（M5-31 悬置上限）。
    pending = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE task_id = ? AND verify_state = 'claimed'",
        (task_id,),
    ).fetchone()[0]
    if pending:
        stuck = conn.execute(
            "SELECT COUNT(*) FROM findings WHERE task_id = ? AND verify_state = 'claimed'"
            " AND sweep_count >= 6",
            (task_id,),
        ).fetchone()[0]
        stuck_note = (
            f"；其中 {stuck} 条裁判已多轮无法判真（重复申报同名 finding 无益，"
            "请补充可重放证据 / 配置 [[ground_truth]] / 开启 [probe] / 人工 manual）"
            if stuck
            else ""
        )
        failures.append(
            f"claimed 悬置 {pending} 条未收口（需 verified/rejected 终态；"
            "若无 R4/探针/只读重放，写操作 claim 无法验真——检查 profile 的 "
            f"ground_truth 或开启 [probe]）{stuck_note}"
        )

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

    # 条 6：步骤全部完成（M5-19，设计文档 L21"步骤是否都完成了"）。
    # 正常路径不会出现非 ok 步（voided 只发生在 schema 炸了并走异常出口时），
    # 这条是机制兜底：账上留着 voided/aborted 步就说明本 run 不完整，不许正常出场。
    bad_steps = conn.execute(
        "SELECT step_no, status FROM steps WHERE task_id = ? AND status <> 'ok'"
        " ORDER BY step_no LIMIT 5",
        (task_id,),
    ).fetchall()
    if bad_steps:
        failures.append(
            f"步骤未全部完成：{len(bad_steps)} 步非 ok"
            f"（如 step {bad_steps[0]['step_no']} status={bad_steps[0]['status']}）"
        )

    # 条 6 补充（M5-27）：收工步零进展——最后一次 done 落在"工具全失败"的步上。
    # 为什么只判最近一步、不判全 run：出场问的是"你凭什么叫完成"，不是"你一路
    # 没摔过"。历史失败步是试错的正常代价（typo 改对即继续），追罚会让一次
    # 失败永久封印出场；而"最后一步全失败还声称收工"正是实测的假完成形态。
    # denied 步不算零进展（与失速计同口径）：护栏拒绝 = 工具压根没执行，
    # 病因在授权不在打法，把它记成"没干成"只会误导出场结论。
    barren = [
        int(row["step_no"])
        for row in conn.execute(
            "SELECT s.step_no FROM steps s WHERE s.task_id = ? AND s.status = 'ok'"
            " AND s.step_no = (SELECT MAX(step_no) FROM steps"
            "                  WHERE task_id = ? AND status = 'ok')"
            " AND EXISTS (SELECT 1 FROM tickets t WHERE t.step_id = s.id"
            "             AND t.summary NOT LIKE 'E_GUARD_DENY%')"
            " AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.step_id = s.id AND t.exit_code = 0)",
            (task_id, task_id),
        ).fetchall()
    ]
    if barren:
        failures.append(f"收工步零进展（step {barren[0]}）：本步工具全部失败，不许正常收工")

    return ExitVerdict(passed=not failures, failures=failures)


def _graph_gaps(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """小票/双图一致性（M5-15）：ok 步 ↔ 探索图节点、verified finding ↔ 证据图节点。

    为什么用"图上是否已有该类节点"作闸：M5-15 之前落账的老库没有 finding 节点，
    直接倒判会把历史账全判红。引擎生产的任务必然有 step 节点（M4-6 起每步写），
    所以当 step 节点存在时按严格口径判；findings 侧同理（进场会先补节点）。
    """
    gaps: list[str] = []
    step_nodes = conn.execute(
        "SELECT COUNT(*) FROM graph_nodes WHERE task_id = ? AND kind = 'step'", (task_id,)
    ).fetchone()[0]
    if step_nodes:
        missing_steps = conn.execute(
            "SELECT s.step_no FROM steps s WHERE s.task_id = ? AND s.status = 'ok'"
            " AND NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.task_id = s.task_id"
            " AND g.kind = 'step' AND g.key = 'step-' || s.step_no)"
            " ORDER BY s.step_no LIMIT 5",
            (task_id,),
        ).fetchall()
        if missing_steps:
            gaps.append(
                f"账本与探索图不一致：{len(missing_steps)} 个 ok 步缺 step 节点"
                f"（如 step {missing_steps[0]['step_no']}）"
            )
    finding_nodes = conn.execute(
        "SELECT COUNT(*) FROM graph_nodes WHERE task_id = ? AND kind = 'finding'", (task_id,)
    ).fetchone()[0]
    if finding_nodes:
        missing_findings = conn.execute(
            "SELECT f.title FROM findings f WHERE f.task_id = ?"
            " AND f.verify_state = 'verified'"
            " AND NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.task_id = f.task_id"
            " AND g.kind = 'finding' AND g.key = 'finding-' || f.id) LIMIT 5",
            (task_id,),
        ).fetchall()
        if missing_findings:
            gaps.append(
                f"账本与证据图不一致：{len(missing_findings)} 条 verified finding 缺节点"
                f"（如「{missing_findings[0]['title']}」）"
            )
    return gaps


class ExitChecklist:
    """ExitChecker 协议的实现体（M1 三条 + M5 交付/规划/双图/步骤完成六验）。"""

    def __init__(self, acceptance: Sequence[AcceptanceRule] = ()) -> None:
        """acceptance 由装配层从 profile 映射（finalize 不 import judge：分层单向）。"""
        self._acceptance = tuple(acceptance)

    def check(self, conn: sqlite3.Connection, task_id: str) -> ExitVerdict:
        """委托纯函数 run_checklist（同类无状态，纯函数便于单测直调）。"""
        return run_checklist(conn, task_id, acceptance=self._acceptance)


__all__ = ["AcceptanceRule", "ExitChecklist", "run_checklist"]
