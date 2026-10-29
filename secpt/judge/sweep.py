"""sweep 裁决收口（W3-2 主入口，M5 补 R2/R3）：R0 → R4 → R1 → R2 → R3，未命中共保 claimed。

纪律（蓝本 WF4）：
- 裁判不 throw 只改账——sweep 对坏 claim 走 rejected 落账，绝不抛错打断主循环；
  只有账本层错误（LedgerError 家族）原样上抛，由 loop 的异常出口兜（§9）。
- sweep 只读 claimed 行做裁决（task_findings 过滤），天然幂等：终态行
  （verified/rejected）永远不进来，第二次 sweep 轮空——同 set_finding_state
  的"终态禁改判"双保险。
- audit actor=judge 是裁决的背书痕：每条被裁决的 finding 各一行。悬置的各道
  裁判各自留一行（"没有结论"也要可证，不是吞掉）。

管线（成本从低到高，逐级上；任一道出结论即停）：
1. R0 自证：锚点必须真实存在（文件非空 / observations 行有实质事件）；
2. R4 oracle：profile 的 ground_truth 命中即 verified(oracle)；
3. R1 重放：只在注入 ReplayExecutor 且锚在票面上时跑（只读复跑比对）；
4. R2 双路径：存在"同结论 + 不同工具 + 不同证据锚"且已 verified 的对侧；
5. R3 反证：流量锚（obs:）有实质事件 → 反证失败 → verified(negative)。

M5 变更说明（非破坏性）：R2/R3 在此前是"图纸位悬置"，现在正式接手 R1 之后的
悬案；不注入 replay 时 R1 不跑（M2/M3 守恒机位不变），但 R2/R3 是纯读账推理，
照常工作——单 claim 场景行为与旧版逐字节一致（没有对侧/流量锚时仍悬置）。
"""

from __future__ import annotations

import sqlite3

from secpt.judge.profile import JudgeProfile
from secpt.judge.r0_self import r0
from secpt.judge.r4_oracle import Match, r4
from secpt.judge.r23 import r2, r3
from secpt.judge.r123 import ReplayExecutor, r1
from secpt.store import ledger
from secpt.store.ledger import FindingRow

_OBS_PREFIX = "obs:"


def sweep(
    conn: sqlite3.Connection,
    task_id: str,
    profile: JudgeProfile,
    *,
    replay: ReplayExecutor | None = None,
    environment: str | None = None,
) -> list[FindingRow]:
    """对 task 的全部 claimed findings 收口一轮裁决，返回本轮被裁决的行。

    - profile 关闭：no-op 返回 []（judge.enabled=false 时 judge 不上班）；
    - R0 不过 → rejected（method=None，无依据不编）；
    - R4 命中 → verified(oracle)，票盒不记票（R4 是更硬的背书）；
    - R1 注入且票面锚 → 重放（一致 verified(rerun)、不一致降票悬置）；
    - R2 双路径 / R3 反证：给 R1 之后的悬案出口；
    - 都没命中 → 保持 claimed（悬置不审，各道已留痕）。
    """
    if not profile.enabled:
        return []
    claimed = ledger.task_findings(conn, task_id, verify_state="claimed")
    verdicts: list[FindingRow] = []
    for finding in claimed:
        if not r0(conn, finding):
            verdicts.append(ledger.set_finding_state(conn, finding.id, "rejected", method=None))
            ledger.audit(
                conn,
                task_id,
                "judge",
                f"裁决 {finding.id[:8]}: rejected（R0 判零：{finding.evidence_ref or '无锚点'}）",
            )
            continue
        hit: Match | None = r4(finding, profile)
        if hit is not None:
            verdicts.append(ledger.set_finding_state(conn, finding.id, "verified", method="oracle"))
            ledger.audit(
                conn, task_id, "judge", f"裁决 {finding.id[:8]}: verified(oracle) 命中 {hit.id}"
            )
            continue
        verdict: FindingRow | None = None
        if replay is not None and not finding.evidence_ref.strip().startswith(_OBS_PREFIX):
            verdict = r1(conn, finding, replay, environment)
        if verdict is None:
            verdict = r2(conn, finding)
        if verdict is None:
            verdict = r3(conn, finding)
        if verdict is not None:
            verdicts.append(verdict)
    return verdicts


__all__ = ["sweep"]
