"""经验库（WF10：全库谁都能读/唯 LLMB 经本收口写）。

设计动机（红线怎么落地而不是喊口号）：
- "唯 LLMB 写"的执行者不是人的自觉，而是收口写入口：任何想把经验经验落账的
  器官/脚本都只能调 :func:`record_experience`——锚点不可证（raw_ref 空白）的条目
  在这里被当场拒落并留一行丢弃审计，经验库因此永远不会填满自我肯定话术。
- 读不需要特权：:func:`fetch_replay` 是普通纯读查询，注意力打包/回放/面板随便用。
- M3-4 判票接线：票盒写口 promote_claimed/decay_votes 收在本层（蓝本 §3.2
  "本 run 复核命中经验可升 verified" 的豁口正式兑现，audit actor=judge 背书——
  与"唯 LLMB 写"红线的张力由蓝本载明豁口 + 0003 UPDATE 放行触发器兜底），
  命根 useful_count 从此如实记账：重放通过 +1 并升 verified，不匹配 −1 地板 0。
"""

from __future__ import annotations

import sqlite3
import time
import uuid

from pydantic import BaseModel

from secpt.errors import LedgerError
from secpt.store import ledger
from secpt.store.ledger import _maybe_commit  # 与 graph.py 同款：写函数统一收口

_VERIFY_STATES = frozenset({"claimed", "verified", "rejected"})


class ExperienceRow(BaseModel):
    """experience 行强类型视图（八字段）：environment/verify_state/useful_count 是命根。"""

    id: str
    task_id: str
    environment: str
    verify_state: str
    useful_count: int
    summary: str
    command: str | None
    raw_ref: str
    created_at: int


def record_experience(
    conn: sqlite3.Connection,
    task_id: str,
    environment: str,
    verify_state: str,
    summary: str,
    command: str | None,
    raw_ref: str,
) -> ExperienceRow | None:
    """经验唯一写入口（audit actor=llmb）。锚点缺失的条目拒落：不抛错、留丢弃审计。

    Args:
        verify_state: 由调用方（Distiller）按事实 derive，LLM 无权改判；
        raw_ref: 可证锚点（小票/runs 路径）。空白视为"这经验无法盘问"→ 拒落。

    Returns:
        落账成功的行；锚点被拒时返回 None（调用方计入丢弃数即可）。

    Raises:
        LedgerError: verify_state 非法（在第一道闸拒绝，schema CHECK 兜底）。
    """
    if verify_state not in _VERIFY_STATES:
        raise LedgerError(
            "非法 verify_state",
            context={"task_id": task_id, "verify_state": verify_state},
        )
    if not raw_ref.strip():
        ledger.audit(conn, task_id, "llmb", f"经验条目锚点缺失（raw_ref 空）丢弃：{summary[:60]}")
        return None
    row = ExperienceRow(
        id=uuid.uuid4().hex,
        task_id=task_id,
        environment=environment,
        verify_state=verify_state,
        useful_count=0,
        summary=summary,
        command=command,
        raw_ref=raw_ref,
        created_at=int(time.time()),
    )
    try:
        conn.execute(
            "INSERT INTO experience (id, task_id, environment, verify_state, useful_count,"
            " summary, command, raw_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.id,
                row.task_id,
                row.environment,
                row.verify_state,
                row.useful_count,
                row.summary,
                row.command,
                row.raw_ref,
                row.created_at,
            ),
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("experience 写入失败", context={"task_id": task_id}) from exc
    ledger.audit(
        conn,
        task_id,
        "llmb",
        f"经验落账：[{verify_state}] {summary[:60]}（锚 {raw_ref[:80]}）",
    )
    return row


def promote_claimed(
    conn: sqlite3.Connection, task_id: str, environment: str, anchor: str
) -> list[ExperienceRow]:
    """票盒写口·升（判票）：R1 重放通过，同环境同锚的 claimed 经验升级记票。

    锚点判同口径（拍板）：``raw_ref == anchor`` 精确等值——同一条证据链就是同
    一条经验，不做模糊口径。只升 claimed（verified 不重复记票），
    UPDATE 同步推进两列（verify_state→verified 且 useful_count+1，一次落账
    一个原子动作）；UPDATE 放行是 0003 迁移预留口，此处为唯一启用位。

    Returns:
        升级后的行（含 +1 后的新票数）；锚点空白 = 纯 no-op 返 []（不炸不编账）。

    Raises:
        LedgerError: sqlite 故障原样分层上抛（库层不吞错）。
    """
    if not anchor.strip():
        return []
    try:
        matched = conn.execute(
            "SELECT id FROM experience WHERE task_id = ? AND environment = ?"
            " AND raw_ref = ? AND verify_state = 'claimed'",
            (task_id, environment, anchor),
        ).fetchall()
        for r in matched:
            conn.execute(
                "UPDATE experience SET verify_state = 'verified',"
                " useful_count = useful_count + 1 WHERE id = ?",
                (r["id"],),
            )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("experience 判票失败", context={"task_id": task_id}) from exc
    if not matched:
        return []
    ledger.audit(
        conn,
        task_id,
        "judge",
        f"判票 promote：{len(matched)} 条经验升 verified 且 +1 票（锚 {anchor[:80]}）",
    )
    rows = conn.execute(
        "SELECT * FROM experience WHERE id IN"
        f" ({','.join('?' * len(matched))}) ORDER BY created_at, rowid",
        tuple(r["id"] for r in matched),
    ).fetchall()
    return [ExperienceRow.model_validate(dict(r)) for r in rows]


def decay_votes(conn: sqlite3.Connection, task_id: str, environment: str, anchor: str) -> int:
    """票盒写口·降（判票）：R1 重放不匹配，同环境同锚的经验降票（地板 0）。

    为什么减票不动 verify_state：重放对立意味着"这条经验不可靠"，还没到
    "这不是真本事"——直接踢 verified 是翻案，会计上留给 Distiller 与直觉对照；
    useful_count 用 SQL 内 ``MAX(-1, 0)`` 钉地板，票空再减也是 no-op，
    有条目命中才落一行降票审计（audit actor=judge，票盒动作全部可证）。

    Returns:
        命中并处理的经验条数（锚点空白或无匹配 = 0，纯 no-op 不审计）。

    Raises:
        LedgerError: sqlite 故障原样分层上抛（库层不吞错）。
    """
    if not anchor.strip():
        return 0
    try:
        cursor = conn.execute(
            "UPDATE experience SET useful_count = MAX(useful_count - 1, 0)"
            " WHERE task_id = ? AND environment = ? AND raw_ref = ?",
            (task_id, environment, anchor),
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("experience 降票失败", context={"task_id": task_id}) from exc
    if cursor.rowcount <= 0:
        return 0
    ledger.audit(
        conn,
        task_id,
        "judge",
        f"降票（-1，地板 0）：{cursor.rowcount} 条经验投票下调（锚 {anchor[:80]}）",
    )
    return cursor.rowcount


def fetch_replay(
    conn: sqlite3.Connection, environment: str, *, top_k: int = 5
) -> list[ExperienceRow]:
    """同环境经验回放（纯读）：只收 verified、投票多优先（Barrett 式收紧）。

    三态规则在 SQL 一层收死：verified=凭据进包 / claimed=线索不进包 / rejected=
    不进包——M3-4 拍板④兑现 §63：判票接线后，回放位要的是"验证过的真本事"，
    而不是待验证的直觉（attention 投影层还会再挡一次，双闸互为备份）。
    """
    rows = conn.execute(
        "SELECT * FROM experience WHERE environment = ? AND verify_state = 'verified'"
        " ORDER BY useful_count DESC, created_at DESC, rowid DESC LIMIT ?",
        (environment, top_k),
    ).fetchall()
    return [ExperienceRow.model_validate(dict(r)) for r in rows]


__all__ = [
    "ExperienceRow",
    "decay_votes",
    "fetch_replay",
    "promote_claimed",
    "record_experience",
]
