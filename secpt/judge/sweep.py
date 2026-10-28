"""sweep 裁决收口（W3-2 主入口）：R0 → R4 → 未命中共保 claimed。

纪律（蓝本 WF4）：
- 裁判不 throw 只改账——sweep 对坏 claim 走 rejected 落账，绝不抛错打断主循环；
  只有账本层错误（LedgerError 家族）原样上抛，由 loop 的异常出口兜（§9）。
- sweep 只读 claimed 行做裁决（task_findings 过滤），天然幂等：终态行
  （verified/rejected）永远不进来，第二次 sweep 轮空——同 set_finding_state
  的"终态禁改判"双保险。
- audit actor=judge 是裁决的背书痕：每条被裁决的 finding 各一行；R0 过 R4
  不中 = 悬置 M3，不裁决不审计（沉默在这里不是吞错，是没有结论）。
"""

from __future__ import annotations

import sqlite3

from secpt.judge.profile import JudgeProfile
from secpt.judge.r0_self import r0
from secpt.judge.r4_oracle import Match, r4
from secpt.store import ledger
from secpt.store.ledger import FindingRow


def sweep(conn: sqlite3.Connection, task_id: str, profile: JudgeProfile) -> list[FindingRow]:
    """对 task 的全部 claimed findings 收口一轮裁决，返回本轮被裁决的行。

    - profile 关闭：no-op 返回 []（judge.enabled=false 时 judge 不上班）；
    - R0不过 rejected → rejected（method=None，无依据不编）；
    - R0 过 + R4 命中 → verified（method="oracle"）；
    - 都没进入 → 保持 claimed（悬置 M3）。
    """
    if not profile.enabled:
        return []
    claimed = ledger.task_findings(conn, task_id, verify_state="claimed")
    verdicts: list[FindingRow] = []
    for finding in claimed:
        hit: Match | None = None
        if r0(finding):
            hit = r4(finding, profile)
            if hit is None:
                continue  # R0 过 R4 不中：悬置 M3（R1-R3 接手），
        state = "verified" if hit else "rejected"
        method = "oracle" if hit else None
        what = (
            f"裁决 {finding.id[:8]}: verified(oracle) 命中 {hit.id}"
            if hit
            else f"裁决 {finding.id[:8]}: rejected（R0 判零：{finding.evidence_ref or '无锚点'}）"
        )
        verdicts.append(ledger.set_finding_state(conn, finding.id, state, method=method))
        ledger.audit(conn, task_id, "judge", what)
    return verdicts


__all__ = ["sweep"]
