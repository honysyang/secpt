"""双图（资产图 + 探索图）最简读写 pilot（W2-2，施工蓝本 §3.3/§5）。

为什么只有五个函数：M1 的双图只承担两件事——
1) 把"这步碰到的实体与关系"存档进黑板：同键 upsert version+1（不覆盖来看过的事，
   又不让重复落键滋生重复行——version 防漂移是文档钉死的语义）；
2) 给注意力包投一份一屏摘要（graph_summary），让主 LLM 不失忆。
其余图查询/裁剪/可视化全部留给后续里程碑，不做文档之外的能力。

读路径纯 SELECT；写路径与 ledger 同纪律：Batched 服从外层 write_batch 边界，
否则写一笔交一笔；sqlite 错误统一包 LedgerError 上抛，库层不吞错。
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

# 节点 kind 四值即 DDL 的 CHECK；摘要渲染与取数按此固定顺序，输出稳定可测。
_KINDS: tuple[str, ...] = ("asset", "hypo", "step", "finding")


class GraphNodeRow(BaseModel):
    """graph_nodes 行强类型视图（kind 四值：asset/hypo/step/finding）。"""

    id: str
    task_id: str
    kind: str
    key: str
    props_json: str
    version: int
    updated_at: int


class GraphEdgeRow(BaseModel):
    """graph_edges 行强类型视图（kind 不设 CHECK——图的边型后续里程碑再收口）。"""

    id: str
    task_id: str
    src: str
    dst: str
    kind: str
    version: int
    updated_at: int


def _now() -> int:
    return int(time.time())


def put_node(
    conn: sqlite3.Connection, task_id: str, kind: str, key: str, props: Mapping[str, Any]
) -> GraphNodeRow:
    """落一个节点：首插 version=1，同 (task_id, kind, key) 再落 version+1 并刷新 props。

    先查后写而不是 INSERT..ON CONFLICT：DDL 没给 (task_id, kind, key) 唯一约束，
    不为 pilot 私改 schema（baseline 红线：结构变更必须走 migration）。
    """
    if kind not in _KINDS:
        raise LedgerError("graph_nodes kind 非法", context={"kind": kind, "allowed": list(_KINDS)})
    props_json = json.dumps(dict(props), ensure_ascii=False, sort_keys=True)
    now = _now()
    try:
        row = conn.execute(
            "SELECT id, version FROM graph_nodes WHERE task_id = ? AND kind = ? AND key = ?",
            (task_id, kind, key),
        ).fetchone()
        if row is None:
            node = GraphNodeRow(
                id=uuid.uuid4().hex,
                task_id=task_id,
                kind=kind,
                key=key,
                props_json=props_json,
                version=1,
                updated_at=now,
            )
            conn.execute(
                "INSERT INTO graph_nodes (id, task_id, kind, key, props_json, version,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    node.id,
                    node.task_id,
                    node.kind,
                    node.key,
                    node.props_json,
                    node.version,
                    node.updated_at,
                ),
            )
        else:
            node = GraphNodeRow(
                id=row["id"],
                task_id=task_id,
                kind=kind,
                key=key,
                props_json=props_json,
                version=row["version"] + 1,
                updated_at=now,
            )
            conn.execute(
                "UPDATE graph_nodes SET props_json = ?, version = ?, updated_at = ? WHERE id = ?",
                (node.props_json, node.version, node.updated_at, node.id),
            )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError("graph_nodes 写入失败", context={"kind": kind, "key": key}) from exc
    return node


def put_edge(conn: sqlite3.Connection, task_id: str, src: str, dst: str, kind: str) -> GraphEdgeRow:
    """落一条边：语义与 put_node 相同（同键 version+1，src/dst 是调用方给的端点键）。"""
    now = _now()
    try:
        row = conn.execute(
            "SELECT id, version FROM graph_edges"
            " WHERE task_id = ? AND src = ? AND dst = ? AND kind = ?",
            (task_id, src, dst, kind),
        ).fetchone()
        if row is None:
            edge = GraphEdgeRow(
                id=uuid.uuid4().hex,
                task_id=task_id,
                src=src,
                dst=dst,
                kind=kind,
                version=1,
                updated_at=now,
            )
            conn.execute(
                "INSERT INTO graph_edges (id, task_id, src, dst, kind, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    edge.id,
                    edge.task_id,
                    edge.src,
                    edge.dst,
                    edge.kind,
                    edge.version,
                    edge.updated_at,
                ),
            )
        else:
            edge = GraphEdgeRow(
                id=row["id"],
                task_id=task_id,
                src=src,
                dst=dst,
                kind=kind,
                version=row["version"] + 1,
                updated_at=now,
            )
            conn.execute(
                "UPDATE graph_edges SET version = ?, updated_at = ? WHERE id = ?",
                (edge.version, edge.updated_at, edge.id),
            )
        _maybe_commit(conn)
    except sqlite3.Error as exc:
        raise LedgerError(
            "graph_edges 写入失败", context={"src": src, "dst": dst, "kind": kind}
        ) from exc
    return edge


def task_nodes(
    conn: sqlite3.Connection, task_id: str, *, kind: str | None = None
) -> list[GraphNodeRow]:
    """读一个任务的节点（可按 kind 过滤；按 updated_at 升序，输出稳定可测）。"""
    sql = "SELECT * FROM graph_nodes WHERE task_id = ?"
    params: list[Any] = [task_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY updated_at, id"
    rows = conn.execute(sql, params).fetchall()
    return [
        GraphNodeRow(
            id=r["id"],
            task_id=r["task_id"],
            kind=r["kind"],
            key=r["key"],
            props_json=r["props_json"],
            version=r["version"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def task_edges(conn: sqlite3.Connection, task_id: str) -> list[GraphEdgeRow]:
    """读一个任务的边（按 updated_at 升序）。"""
    rows = conn.execute(
        "SELECT * FROM graph_edges WHERE task_id = ? ORDER BY updated_at, id", (task_id,)
    ).fetchall()
    return [
        GraphEdgeRow(
            id=r["id"],
            task_id=r["task_id"],
            src=r["src"],
            dst=r["dst"],
            kind=r["kind"],
            version=r["version"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def finding_key(finding_id: str) -> str:
    """证据图里 finding 节点的键（与出场清单的一致性校验共用同一约定）。"""
    return f"finding-{finding_id}"


def put_finding_node(
    conn: sqlite3.Connection,
    task_id: str,
    finding_id: str,
    *,
    title: str,
    evidence_ref: str,
    actor: str,
) -> GraphNodeRow:
    """落一条证据图节点（M5-15）：finding 是"证据图"里的实体。

    双图口径：``step`` 节点串成探索图（主循环每步一条），``finding`` 节点是证据图
    （每条 claim 一条，锚点随节点存 props 便于面板/一致性校验）。同一 finding 重复
    落键走 version+1（幂等重放安全）。
    """
    return put_node(
        conn,
        task_id,
        "finding",
        finding_key(finding_id),
        {"title": title[:80], "evidence_ref": evidence_ref[:200], "actor": actor},
    )


def ensure_step_nodes(conn: sqlite3.Connection, task_id: str) -> int:
    """补齐缺失的探索图 step 节点（幂等）：返回本次补了几条。

    M4-6 起主循环每步都写 step 节点；本函数只兜"历史账/半截事务"的缺口——
    续跑一个旧 run 时若水位线之前的步没有节点，出场清单①会倒判红，这里先对齐。
    """
    rows = conn.execute(
        "SELECT s.step_no, s.llm_output FROM steps s WHERE s.task_id = ? AND s.status = 'ok'"
        " AND NOT EXISTS (SELECT 1 FROM graph_nodes g WHERE g.task_id = s.task_id"
        " AND g.kind = 'step' AND g.key = 'step-' || s.step_no)"
        " ORDER BY s.step_no",
        (task_id,),
    ).fetchall()
    for row in rows:
        thought = ""
        try:
            payload = json.loads(str(row["llm_output"]))
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            thought = str(payload.get("thought") or "")
        put_node(
            conn,
            task_id,
            "step",
            f"step-{row['step_no']}",
            {"thought": thought[:80], "backfilled": True},
        )
    return len(rows)


def ensure_finding_nodes(conn: sqlite3.Connection, task_id: str) -> int:
    """补齐缺失的证据图节点（幂等）：返回本次补了几条。

    为什么需要：worker 直写的 finding 与 M5-15 之前的历史 finding 可能没有节点；
    出场清单①要"小票/双图一致"，进场/续跑时补一次，保证账本与证据图对齐。
    """
    rows = conn.execute(
        "SELECT f.id, f.title, f.evidence_ref, f.actor FROM findings f"
        " WHERE f.task_id = ? AND NOT EXISTS ("
        "   SELECT 1 FROM graph_nodes g WHERE g.task_id = f.task_id"
        "   AND g.kind = 'finding' AND g.key = 'finding-' || f.id)"
        " ORDER BY f.rowid",
        (task_id,),
    ).fetchall()
    for row in rows:
        put_finding_node(
            conn,
            task_id,
            str(row["id"]),
            title=str(row["title"]),
            evidence_ref=str(row["evidence_ref"]),
            actor=str(row["actor"]),
        )
    return len(rows)


def graph_summary(conn: sqlite3.Connection, task_id: str) -> str:
    """渲染给注意力包的一屏摘要：kind 计数 + 边数 + 最近五个键。

    空图返回空串——attention.render() 以"非空才出现双图段"为节制，不给模型看空气。
    """
    tot_edges = conn.execute(
        "SELECT COUNT(*) FROM graph_edges WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    counts: dict[str, int] = {k: 0 for k in _KINDS}
    total = 0
    for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM graph_nodes WHERE task_id = ? GROUP BY kind",
        (task_id,),
    ):
        counts[r["kind"]] = r["n"]
        total += r["n"]
    if total == 0 and tot_edges == 0:
        return ""
    parts = ", ".join(f"{k}={counts[k]}" for k in _KINDS if counts[k])
    recent = conn.execute(
        "SELECT key FROM graph_nodes WHERE task_id = ? ORDER BY updated_at DESC, id DESC LIMIT 5",
        (task_id,),
    ).fetchall()
    keys = "、".join(r["key"] for r in recent)
    return f"节点 {total}（{parts}）；边 {tot_edges} 条" + (f"；最近: {keys}" if keys else "")


__all__ = [
    "GraphEdgeRow",
    "GraphNodeRow",
    "ensure_finding_nodes",
    "ensure_step_nodes",
    "finding_key",
    "graph_summary",
    "put_edge",
    "put_finding_node",
    "put_node",
    "task_edges",
    "task_nodes",
]
