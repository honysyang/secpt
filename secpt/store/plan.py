"""规划对象（M5-5）：主 LLM 的 plan_updates 落成账本事实，注意力包读回"当前规划"。

设计动机（设计文档 L9-10 / L16）：
- 此前 ``plan_updates`` 只是 StepOutput 里的一个字段，主循环从不消费——主 LLM 列的
  A/B/C 规划说完就丢，下一步看到的是一句硬编码占位。规划不落账，"规划状态实时更新"
  与"根据双图状态评估整体规划"都无从谈起。
- 本模块把规划变成 task 级的账本事实（baseline 的 plans 表早有此位，M5 才接上）：
  每条规划一行，version 是"规划修订号"（同一批新增共享一号），供注意力包与教练
  看到同一份规划文本。
- 写入口唯一：:func:`add` / :func:`complete` 是仅有的两个写口；读路径纯 SELECT。
  status 推进（M5-9）：主 LLM 用 ``plan_done`` / ``plan_cancel`` 声明收口，本模块
  负责"引用解析 + 状态迁移"——引用既认规划短 id（注意力包渲染 ``[id=xxxxxxxx]``），
  也认原文（模型复述原句同样有效），歧义前缀不猜（宁可报未匹配也不误收口）。

去重口径：内容（strip 后）相同且仍 active 的规划不重复落行——模型复述同一规划
不该把账本撑成流水账；已 done/cancelled 的旧条目允许同名重开。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from secpt.errors import LedgerError
from secpt.store.ledger import _maybe_commit

# 注意力包最多展示的规划条数（防规划无限增补把包撑爆）。
_RENDER_LIMIT = 12
_EMPTY_PLAN = "（暂无规划：主 LLM 尚未通过 plan_updates 提出）"
_PLAN_STATUSES = frozenset({"done", "cancelled"})
# 短 id 前缀匹配的最小长度：太短的串会撞上多条规划，宁可不匹配。
_MIN_ID_PREFIX = 6


class PlanRow(BaseModel):
    """plans 行强类型视图（status：active/done/cancelled/failed）。"""

    id: str
    task_id: str
    step_no: int
    content: str
    parent_id: str | None
    status: str
    version: int
    updated_at: int


def active(conn: sqlite3.Connection, task_id: str) -> list[PlanRow]:
    """当前生效规划（按修订号、提出步序稳定排序）。纯读。"""
    rows = conn.execute(
        "SELECT * FROM plans WHERE task_id = ? AND status = 'active'"
        " ORDER BY version, step_no, rowid",
        (task_id,),
    ).fetchall()
    return [PlanRow.model_validate(dict(r)) for r in rows]


def latest_id(conn: sqlite3.Connection, task_id: str) -> str | None:
    """最近一条规划行 id（教练改规划时作 parent_id 指向上版）。纯读。"""
    row = conn.execute(
        "SELECT id FROM plans WHERE task_id = ? ORDER BY rowid DESC LIMIT 1", (task_id,)
    ).fetchone()
    return str(row["id"]) if row is not None else None


def add(
    conn: sqlite3.Connection,
    task_id: str,
    step_no: int,
    contents: Sequence[str],
    *,
    parent_id: str | None = None,
) -> list[PlanRow]:
    """把一批规划文本落成新修订；返回本次真正新增的行（空/重复一律 []）。

    Args:
        step_no: 提出这一步的步号（注意力包按它标注"第几步提出"）；
        parent_id: 教练改规划时指向上版；主 LLM 自陈增补时留 None。

    Raises:
        LedgerError: sqlite 写入失败（库层不吞错）。
    """
    cleaned: list[str] = []
    for raw in contents:
        text = str(raw).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return []
    existing = {row.content for row in active(conn, task_id)}
    fresh = [content for content in cleaned if content not in existing]
    if not fresh:
        return []
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM plans WHERE task_id = ?", (task_id,)
    ).fetchone()
    version = int(version_row["v"]) + 1
    now = int(time.time())
    rows: list[PlanRow] = []
    try:
        for content in fresh:
            item = PlanRow(
                id=uuid.uuid4().hex,
                task_id=task_id,
                step_no=step_no,
                content=content,
                parent_id=parent_id,
                status="active",
                version=version,
                updated_at=now,
            )
            conn.execute(
                "INSERT INTO plans (id, task_id, step_no, content, parent_id, status,"
                " version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.task_id,
                    item.step_no,
                    item.content,
                    item.parent_id,
                    item.status,
                    item.version,
                    item.updated_at,
                ),
            )
            rows.append(item)
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError(
            "plans 写入失败", context={"task_id": task_id, "step_no": step_no}
        ) from exc
    return rows


def render(conn: sqlite3.Connection, task_id: str) -> str:
    """渲染进注意力包的规划段：修订号 + 条数 + 逐条（短 id + 提出步号 + 原文）。"""
    rows = active(conn, task_id)
    if not rows:
        return _EMPTY_PLAN
    version = max(row.version for row in rows)
    shown = rows[-_RENDER_LIMIT:]
    lines = [f"规划 v{version}（active {len(rows)} 条）"]
    lines += [f"- [id={row.id[:8]} · step {row.step_no}] {row.content}" for row in shown]
    if len(rows) > len(shown):
        lines.append(f"（仅显示最近 {len(shown)} 条，更早条目已折叠）")
    return "\n".join(lines)


def complete(
    conn: sqlite3.Connection,
    task_id: str,
    refs: Sequence[str],
    *,
    status: str = "done",
) -> tuple[list[PlanRow], list[str]]:
    """把 refs 指向的 active 规划推进到终态（done/cancelled）。

    引用解析顺序：原文精确匹配 → 短 id 前缀（≥6 字符且唯一）。解析不到的一律
    进 unresolved 由调用方落审计——**绝不猜**：把 A 标成完成而实际是 B，比漏标
    更危险。

    Args:
        status: ``"done"``（完成）或 ``"cancelled"``（放弃）；其余 ValueError。

    Returns:
        (被推进的行, 解析失败的引用原文)。
    """
    if status not in _PLAN_STATUSES:
        raise ValueError(f"非法规划终态 {status!r}（可选：{sorted(_PLAN_STATUSES)}）")
    rows = active(conn, task_id)
    by_content = {row.content: row for row in rows}
    now = int(time.time())
    completed: list[PlanRow] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    try:
        for raw in refs:
            ref = str(raw).strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            target = by_content.get(ref) or _by_id_prefix(rows, ref)
            if target is None:
                unresolved.append(ref)
                continue
            conn.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, target.id),
            )
            completed.append(target.model_copy(update={"status": status, "updated_at": now}))
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError(
            "plans 状态推进失败", context={"task_id": task_id, "status": status}
        ) from exc
    return completed, unresolved


def _by_id_prefix(rows: Sequence[PlanRow], ref: str) -> PlanRow | None:
    """短 id 前缀解析：唯一命中才认，歧义/过短一律 None（不猜）。"""
    if len(ref) < _MIN_ID_PREFIX:
        return None
    hits = [row for row in rows if row.id.startswith(ref)]
    return hits[0] if len(hits) == 1 else None


__all__ = ["PlanRow", "active", "add", "complete", "latest_id", "render"]
