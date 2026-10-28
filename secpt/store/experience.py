"""经验库（WF10：全库谁都能读/唯 LLMB 经本收口写）。

设计动机（红线怎么落地而不是喊口号）：
- "唯 LLMB 写"的执行者不是人的自觉，而是收口写入口：任何想把经验经验落账的
  器官/脚本都只能调 :func:`record_experience`——锚点不可证（raw_ref 空白）的条目
  在这里被当场拒落并留一行丢弃审计，经验库因此永远不会填满自我肯定话术。
- 读不需要特权：:func:`fetch_replay` 是普通纯读查询，注意力打包/回放/面板随便用。
- M2 判票不接线：useful_count 恒默认 0，建表保留字段与 UPDATE 放行（0003 触发器），
  等三分判票（W3-2/M3）来了只需推进这一列。
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


def fetch_replay(
    conn: sqlite3.Connection, environment: str, *, top_k: int = 5
) -> list[ExperienceRow]:
    """同环境经验回放（纯读）：投票多的优先，M2 无票时退化为最新优先。

    rejected 在 SQL 一层就被挡住（三态规则：verified=凭据进包 / claimed=线索 /
    rejected=不进包）——attention 投影层还会再挡一次，双闸互为备份。
    """
    rows = conn.execute(
        "SELECT * FROM experience WHERE environment = ? AND verify_state != 'rejected'"
        " ORDER BY useful_count DESC, created_at DESC, rowid DESC LIMIT ?",
        (environment, top_k),
    ).fetchall()
    return [ExperienceRow.model_validate(dict(r)) for r in rows]


__all__ = ["ExperienceRow", "fetch_replay", "record_experience"]
