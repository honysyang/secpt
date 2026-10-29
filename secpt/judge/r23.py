"""R2 双路径 / R3 反证裁判（M5 接线）：写操作不可复跑时的两条独立背书。

为什么这两道要成对出现（蓝本 WF4，设计文档 L73-76）：
- R1 重放只对只读操作成立——"永不 rerun 写操作"是铁律，于是写操作 claim 在
  M3-4 之后一律悬置。R2/R3 就是给这批悬案的出口：
  - **R2 双路径**：同一结论被第二条独立路径（不同工具 + 不同证据锚）也验证过；
  - **R3 反证**：动作发生时取证（observations 流量真相），"证明没发生"失败
    → 最强证实（设计文档 L76：写操作不可复跑的悬案由"动作发生时取证"消解）。

纪律（与 R0/R1/R4 一致）：
- 裁判不 throw 只改账；没有结论就返回 None（悬置），绝不降级成 rejected——
  未命中不是反面证据，只是正面证据还没到。
- 账本层错误（LedgerError 家族）原样上抛，由 loop 的异常出口兜（§9）。

为什么 R2 要求"对侧已 verified"而不是"两条 claim 互相壮胆"：两条都没验证过的
相似说法可能来自同一个幻觉（同模型/同知识源），互证只会放大噪声。只有一条已被
R1/R4 独立确认、另一条来自不同工具与不同证据锚时，"双路径"才真的增加了信息。

为什么 R3 只认流量锚：反证的靶子是"这件事根本没发生"——只有 observations 的
客观事件流能对账（LLM 小票只是"它说它做了"）。文件锚的写操作 claim 没有可对账
的事件流，回 None 悬置，留给后续更强的裁判或人工。
"""

from __future__ import annotations

import json
import re
import sqlite3

from secpt.judge.r0_self import r0
from secpt.store import ledger
from secpt.store.ledger import FindingRow

_OBS_PREFIX = "obs:"
_WHITESPACE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """结论归一：大小写折叠 + 空白折叠（格式差异不算不同结论）。"""
    return _WHITESPACE.sub(" ", text.casefold()).strip()


def _anchor_tool(conn: sqlite3.Connection, finding: FindingRow) -> str | None:
    """证据锚落在哪张小票上（None = 非票面锚，如流量锚/手铺证明）。"""
    if not finding.evidence_ref.strip():
        return None
    row = conn.execute(
        "SELECT tool_name FROM tickets WHERE raw_ref = ? AND task_id = ?"
        " ORDER BY rowid DESC LIMIT 1",
        (finding.evidence_ref, finding.task_id),
    ).fetchone()
    return str(row["tool_name"]) if row is not None else None


def r2(conn: sqlite3.Connection, finding: FindingRow) -> FindingRow | None:
    """R2 双路径裁决：存在"同结论 + 不同工具 + 不同证据锚 + 已 verified"的对侧。

    Args:
        finding: 当前 claimed 行（R0 已过）。

    Returns:
        升级后的行（verified, method="dual-path"）；无合格对侧 → None（悬置不审）。
    """
    key = _norm(finding.title)
    if not key:
        return None
    this_tool = _anchor_tool(conn, finding)
    if this_tool is None:
        return None  # 非票面锚没有"工具路径"可言，交给 R3
    for sibling in ledger.task_findings(conn, finding.task_id, verify_state="verified"):
        if sibling.id == finding.id or _norm(sibling.title) != key:
            continue
        if not sibling.evidence_ref.strip() or sibling.evidence_ref == finding.evidence_ref:
            continue
        sibling_tool = _anchor_tool(conn, sibling)
        if sibling_tool is None or sibling_tool == this_tool:
            continue  # 同工具 = 同一条路，不构成独立路径
        if not r0(conn, sibling):
            continue
        row = ledger.set_finding_state(conn, finding.id, "verified", method="dual-path")
        ledger.audit(
            conn,
            finding.task_id,
            "judge",
            f"裁决 {finding.id[:8]}: verified(dual-path)"
            f"（独立路径 {sibling.id[:8]} 已 verified，工具 {sibling_tool}≠{this_tool}）",
        )
        return row
    return None


def r3(conn: sqlite3.Connection, finding: FindingRow) -> FindingRow | None:
    """R3 反证裁决：流量锚存在实质事件 → 反证失败 → verified(negative)。

    "实质事件"的判定（可一键复算）：observations 行存在、meta_json 能解析成
    非空字典、snapshot_ref 非空——即"动作发生时确实抓到了东西"。空壳行
    （`{}` meta / 空 snapshot）不算，交回悬置。
    """
    anchor = finding.evidence_ref.strip()
    if not anchor.startswith(_OBS_PREFIX):
        return None  # 文件锚没有客观事件流可对账：反证无靶，悬置
    row = conn.execute(
        "SELECT channel, meta_json, snapshot_ref FROM observations WHERE id = ?",
        (anchor[len(_OBS_PREFIX) :],),
    ).fetchone()
    if row is None:
        return None
    try:
        meta = json.loads(str(row["meta_json"]))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(meta, dict) or not meta:
        return None
    if not str(row["snapshot_ref"]).strip():
        return None
    verified = ledger.set_finding_state(conn, finding.id, "verified", method="negative")
    ledger.audit(
        conn,
        finding.task_id,
        "judge",
        f"裁决 {finding.id[:8]}: verified(negative)"
        f"（动作发生时取证 channel={row['channel']}，反证失败=流量确实存在）",
    )
    return verified


__all__ = ["r2", "r3"]
