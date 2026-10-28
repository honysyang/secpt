"""observations 表读写（M3-3）：LLM 小票之外的客观事件流。

为什么单开一档而不是塞进 ledger：observations 与 tickets/findings 语义不同——
小票是"工具说它做了什么"，观测是"流量实际发生了什么"（§3.2 通道 B，不经
LLM 之手），表结构也走独立 migration（0004）。但写入口纪律不变：黑板写入
只经 store/*，本模块是 observations 的唯一官门（INSERT + LedgerError 包装
+ _maybe_commit，与 ledger 同手式）。

meta 收原始基元参数（channel: str / meta: dict / snapshot_ref: str / ts: int）：
CaptureEvent 是 tools 层模型，store 不 import tools（分层红线单向）——
CaptureEvent → 基元参数的转换归 loop 层。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from secpt.errors import LedgerError
from secpt.store.ledger import _maybe_commit


class ObservationRow(BaseModel):
    """observations 行强类型视图。"""

    id: str
    task_id: str
    run_id: str
    channel: str
    meta_json: str
    snapshot_ref: str
    ts: int


def _now() -> int:
    """统一时间源：epoch 秒（与 ledger._now 同口径）。"""
    return int(time.time())


def append(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: str,
    channel: str,
    meta: Mapping[str, Any],
    snapshot_ref: str,
    *,
    ts: int | None = None,
) -> ObservationRow:
    """observations 唯一写入口：INSERT 一条客观事件。

    channel/snapshot_ref 在官门口就拒空：证据语义字段允许空 = 观测可以凭空
    编造指针，R0 扩判"snapshot 非空"就永远形同虚设。ts 默认落账即时刻。
    """
    channel = channel.strip()
    snapshot_ref = snapshot_ref.strip()
    if not channel:
        raise LedgerError("observations 写入失败：channel 为空", context={"run_id": run_id})
    if not snapshot_ref:
        raise LedgerError("observations 写入失败：snapshot_ref 为空", context={"channel": channel})
    row = ObservationRow(
        id=uuid.uuid4().hex,
        task_id=task_id,
        run_id=run_id,
        channel=channel,
        meta_json=json.dumps(dict(meta), ensure_ascii=False, sort_keys=True),
        snapshot_ref=snapshot_ref,
        ts=ts if ts is not None else _now(),
    )
    try:
        conn.execute(
            "INSERT INTO observations (id, task_id, run_id, channel, meta_json,"
            " snapshot_ref, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row.id,
                row.task_id,
                row.run_id,
                row.channel,
                row.meta_json,
                row.snapshot_ref,
                row.ts,
            ),
        )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError(
            "observations 写入失败", context={"channel": channel, "run_id": run_id}
        ) from exc
    return row


def task_observations(conn: sqlite3.Connection, task_id: str) -> list[ObservationRow]:
    """按 task 读观测流（evidence 命令 / R0 扩判 / 面板展示用）。纯读。"""
    rows = conn.execute(
        "SELECT * FROM observations WHERE task_id = ? ORDER BY ts, rowid", (task_id,)
    ).fetchall()
    return [ObservationRow.model_validate(dict(r)) for r in rows]


__all__ = ["ObservationRow", "append", "task_observations"]
