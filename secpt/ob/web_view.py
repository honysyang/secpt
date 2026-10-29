"""只读 web 面板（W3-4）：账本/双图/报告/audit 流的可视，且无写路径。

为什么做成"每请求一条 SQLite ro 连接 + GET-only 路由"：
- 只读红线不只靠"没有 POST 按钮"保障——面板连接用 ``file:...?mode=ro`` URI 打开，
  缺库不建文件、误操作写不进去（机制而非约定，同 §5 写入口纪律的手式）；
- 全部路由用 ``app.get()`` 注册，Flask 对其余 HTTP 方法自动回 405；
- M2 拍板（§66 前置拍板）：报告块=账本直读 verified-only，不烧 LLM 复跑 Reporter；
  刷新=手动 + SSE 事件流（``/task/<id>/stream`` 轮询 audit_log 增量）。
  "M2 不做并发"约束指引擎侧器官不做并行，HTTP 服务侧的多线程只读 SQL 天然安全。

黑板页布局（§8 可观测性表 M1 行的网页化）：
  任务概要 → 小票卡 → findings 状态机明细（如实）→ 报告块（verified-only）
  → 双图（graph_summary + 节点边明细）→ audit 流（倒序）。

对话过程页（M5，设计文档 L91）：``/task/<id>/chat`` 把 audit 流与 ticket 流
按时间合并成"角色气泡（主LLM/workerA/LLMC…）+ 小票摘要卡"——同一份账本的
另一种投影，不新增任何写路径。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, render_template_string, stream_with_context

from secpt.store import metrics as store_metrics

__all__ = ["create_app"]

# SSE 轮询节奏（秒）：够"准实时"，又不至于把只读库拉痛。
_STREAM_INTERVAL = 2.0

_INDEX_TPL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>secpt 面板 · 任务总览</title>
<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.3rem .6rem}
h1{font-size:1.4rem}</style></head><body>
<h1>任务总览（只读）</h1>
<section><h2>验收杆（M4-6 只读统计）</h2><p>{{ metrics_text }}</p></section>
<table><tr><th>task_id</th><th>input</th><th>goal</th><th>exit_kind</th><th>created_at</th></tr>
{% for t in tasks %}<tr>
<td><a href="/task/{{ t.id }}">{{ t.id[:12] }}</a></td>
<td>{{ t.input_text }}</td><td>{{ t.goal or "-" }}</td>
<td>{{ t.exit_kind or "-" }}</td><td>{{ t.created_at }}</td></tr>
{% endfor %}</table></body></html>"""

_TASK_TPL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>secpt 面板 · {{ task.id[:12] }}</title>
<style>body{font-family:system-ui,sans-serif;max-width:1080px;margin:2rem auto;padding:0 1rem}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.3rem .6rem}
section{margin:1.2rem 0}h2{font-size:1.1rem;border-bottom:1px solid #ddd}
.report li{margin:.2rem 0}</style></head><body><p><a href="/">← 总览</a></p>
<h1>黑板 · {{ task.id[:12] }}（只读）</h1>
<section><h2>任务</h2><table>
<tr><th>goal</th><td>{{ task.goal or "-" }}</td></tr>
<tr><th>input</th><td>{{ task.input_text }}</td></tr>
<tr><th>exit_kind</th><td>{{ task.exit_kind or "-" }}</td></tr>
<tr><th>run</th><td>{{ run.status }} · step_count={{ run.step_count }}</td></tr></table></section>
<section><h2>规划（plans 只读）</h2>
<table><tr><th>v</th><th>step</th><th>status</th><th>content</th></tr>
{% for p in plans %}<tr><td>{{ p.version }}</td><td>{{ p.step_no }}</td>
<td>{{ p.status }}</td><td>{{ p.content }}</td></tr>
{% else %}<tr><td colspan="4">（暂无规划）</td></tr>{% endfor %}</table></section>
<section><h2>小票（最近 {{ tickets|length }} 条）</h2>
<table><tr><th>step</th><th>tool</th><th>exit_code</th><th>summary</th><th>raw_ref</th></tr>
{% for t in tickets %}<tr><td>{{ t.step_id[:12] }}</td><td>{{ t.tool_name }}</td>
<td>{{ t.exit_code if t.exit_code is not none else "-" }}</td>
<td>{{ t.summary }}</td><td>{{ t.raw_ref or "-" }}</td></tr>
{% endfor %}</table></section>
<section><h2>findings 状态机明细（如实）</h2>
<table><tr><th>title</th><th>state</th><th>method</th><th>evidence</th></tr>
{% for f in findings %}<tr><td>{{ f.title }}</td><td>{{ f.verify_state }}</td>
<td>{{ f.method or "-" }}</td><td>{{ f.evidence_ref or "-" }}</td></tr>
{% endfor %}</table></section>
<section><h2>报告（verified-only）</h2>
{% for f in verified %}<li class="report">{{ f.title }} — {{ f.detail }}</li>
{% else %}<p>（无 verified findings，如实照录）</p>{% endfor %}</section>
<section><h2>双图</h2><p>{{ graph_note or "（空图）" }}</p>
<table><tr><th>src</th><th>dst</th><th>kind</th><th>ver</th></tr>
{% for e in edges %}<tr><td>{{ e.src }}</td><td>{{ e.dst }}</td>
<td>{{ e.kind }}</td><td>{{ e.version }}</td></tr>
{% endfor %}</table></section>
<section><h2>audit 流（倒序）</h2>
<table><tr><th>at</th><th>actor</th><th>what</th></tr>
{% for a in audit_log %}<tr><td>{{ a.created_at }}</td><td>{{ a.actor }}</td>
<td>{{ a.what }}</td></tr>
{% endfor %}</table></section>
<p><a href="/task/{{ task.id }}/stream">SSE 事件流</a> ·
<a href="/task/{{ task.id }}/chat">对话过程（角色气泡）</a></p></body></html>"""

_CHAT_TPL = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>secpt 面板 · 对话过程 {{ task.id[:12] }}</title>
<style>body{font-family:system-ui,sans-serif;max-width:880px;margin:2rem auto;padding:0 1rem}
h2{font-size:1.1rem;border-bottom:1px solid #ddd}
.chat{display:flex;flex-direction:column;gap:.5rem}
.bubble{padding:.5rem .8rem;border-radius:.7rem;max-width:80%;line-height:1.5}
.chip{display:inline-block;font-size:.75rem;padding:.05rem .45rem;border-radius:.6rem;
background:#eee;margin-right:.5rem;color:#333}
.main{align-self:flex-start;background:#eef4ff}
.worker{align-self:flex-end;background:#f3f7ee}
.llmc{align-self:flex-start;background:#fff6e6}
.other{align-self:flex-start;background:#f7f7f7}
.card{align-self:flex-start;border:1px solid #d9d9d9;border-left:4px solid #999;
padding:.35rem .7rem;border-radius:.4rem;font-size:.9rem;background:#fff}
.exit{color:#666;margin:0 .4rem}</style></head><body>
<p><a href="/">← 总览</a> · <a href="/task/{{ task.id }}">← 黑板</a></p>
<h1>对话过程 · {{ task.id[:12] }}（只读）</h1>
<p>goal：{{ task.goal or "-" }} · exit_kind：{{ task.exit_kind or "-" }}</p>
<section><h2>角色气泡 + 小票摘要卡（{{ items|length }} 条）</h2>
<div class="chat">
{% for it in items %}{% if it.kind == "ticket" %}
<div class="card"><span class="chip">小票</span><b>{{ it.tool }}</b>
<span class="exit">exit={{ it.exit }}</span>{{ it.text }}</div>
{% else %}
<div class="bubble {{ it.css }}"><span class="chip">{{ it.actor }}</span>{{ it.text }}</div>
{% endif %}{% endfor %}
{% if not items %}<p>（暂无对话记录）</p>{% endif %}
</div></section></body></html>"""

# 角色标签（设计文档 L91：主LLM/workerA/LLMC 各自标签）与气泡样式族。
_ROLE_LABELS: dict[str, str] = {
    "main": "主LLM",
    "workerA": "workerA",
    "workerB": "workerB",
    "llmc": "LLMC 教练",
    "llma": "LLMA 报告",
    "llmb": "LLMB 蒸馏",
    "judge": "裁判",
    "human": "人工",
}
_ROLE_CSS: dict[str, str] = {
    "main": "main",
    "workerA": "worker",
    "workerB": "worker",
    "llmc": "llmc",
}


def _role_label(actor: str) -> str:
    return _ROLE_LABELS.get(actor, actor)


def _role_css(actor: str) -> str:
    return _ROLE_CSS.get(actor, "other")


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """只读连接：mode=ro——缺库不建档、面板侧任何写尝试当场抛错（机制红线）。

    row_factory=Row：查询行统一按列名取（dict(r)），不再出现"既写 SQL 又拼字典"
    的裸元组拼接（同 ledger 行模型手式）。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_ro_or_404(db_path: Path) -> sqlite3.Connection | None:
    """缺库直接 404，不替天建空账本。"""
    if not db_path.exists():
        return None
    return _connect_ro(db_path)


def _open_and_guard(db_path: Path, task_id: str) -> sqlite3.Connection | None:
    """打开 ro 连接并校验 task 存在（同一连接后续继续用，防 TOCTOU）。"""
    conn = _connect_ro_or_404(db_path)
    if conn is None:
        return None
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        conn.close()
        abort(404)  # noqa: B904 — abort 本身即返回
    return conn


def create_app(db_path: str | Path) -> Flask:
    """构造只读面板应用：所有数据直读传入的账本文件（GET-only）。"""
    app = Flask(__name__)
    path = Path(db_path)

    @app.get("/")
    def index() -> str | Response:
        conn = _connect_ro_or_404(path)
        if conn is None:
            abort(404)
        try:
            rows = conn.execute(
                "SELECT id, input_text, goal, exit_kind, created_at FROM tasks"
                " ORDER BY created_at DESC, id DESC"
            ).fetchall()
            metrics_text = store_metrics.render_metrics(conn)  # M4-6 两杆（验收 6 面板面）
        finally:
            conn.close()
        tasks: list[dict[str, Any]] = [dict(r) for r in rows]
        return render_template_string(_INDEX_TPL, tasks=tasks, metrics_text=metrics_text)

    @app.get("/task/<task_id>")
    def task_page(task_id: str) -> str | Response:
        conn = _open_and_guard(path, task_id)
        assert conn is not None  # _open_and_guard 缺库/缺任务已 abort
        try:
            task = dict(
                conn.execute(
                    "SELECT id, input_text, goal, exit_kind, created_at FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            )
            run_row = conn.execute(
                "SELECT id, status, step_count, ended_at FROM runs WHERE task_id = ?"
                " ORDER BY started_at DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            run = dict(run_row) if run_row is not None else {"status": "-", "step_count": 0}
            tickets = [
                dict(r)
                for r in conn.execute(
                    "SELECT step_id, tool_name, exit_code, summary, raw_ref FROM tickets"
                    " WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
                    (task_id,),
                ).fetchall()
            ]
            findings = [
                dict(r)
                for r in conn.execute(
                    "SELECT title, detail, verify_state, method, evidence_ref FROM findings"
                    " WHERE task_id = ? ORDER BY created_at",
                    (task_id,),
                ).fetchall()
            ]
            verified = [f for f in findings if f["verify_state"] == "verified"]
            plans = [
                dict(r)
                for r in conn.execute(
                    "SELECT version, step_no, status, content FROM plans"
                    " WHERE task_id = ? ORDER BY version, step_no, rowid",
                    (task_id,),
                ).fetchall()
            ]
            graph_rows = conn.execute(
                "SELECT key, kind, version FROM graph_nodes WHERE task_id = ?"
                " ORDER BY updated_at DESC",
                (task_id,),
            ).fetchall()
            edges = [
                dict(r)
                for r in conn.execute(
                    "SELECT src, dst, kind, version FROM graph_edges WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
            ]
            graph_note = _graph_note(graph_rows, edges)
            audit_rows = conn.execute(
                "SELECT created_at, actor, what FROM audit_log WHERE task_id = ?"
                " ORDER BY rowid DESC LIMIT 200",
                (task_id,),
            ).fetchall()
            audit_log = [dict(r) for r in audit_rows]
        finally:
            conn.close()
        return render_template_string(
            _TASK_TPL,
            task=task,
            run=run,
            tickets=tickets,
            findings=findings,
            verified=verified,
            plans=plans,
            graph_note=graph_note,
            edges=edges,
            audit_log=audit_log,
        )

    @app.get("/task/<task_id>/chat")
    def chat_page(task_id: str) -> str | Response:
        """对话过程页（M5，设计文档 L91）：audit 流 + 小票流按时间合并成角色气泡。

        投影口径：audit 行 = 某角色的"发言"（气泡），ticket 行 = 小票摘要卡；
        同秒并列时按"谁先发生谁在前"（audit 先于 ticket —— 小票总是某步动作的
        结果）。仍是只读：同一 ro 连接 + GET-only 路由。
        """
        conn = _open_and_guard(path, task_id)
        assert conn is not None  # _open_and_guard 缺库/缺任务已 abort
        try:
            task = dict(
                conn.execute(
                    "SELECT id, input_text, goal, exit_kind, created_at FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            )
            says = conn.execute(
                "SELECT actor, what, created_at, rowid AS rid FROM audit_log"
                " WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            tickets = conn.execute(
                "SELECT tool_name, exit_code, summary, created_at, rowid AS rid FROM tickets"
                " WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        items: list[dict[str, Any]] = []
        for row in says:
            actor = str(row["actor"])
            items.append(
                {
                    "created_at": int(row["created_at"]),
                    "seq": int(row["rid"]),
                    "kind": "say",
                    "actor": _role_label(actor),
                    "css": _role_css(actor),
                    "text": str(row["what"]),
                }
            )
        for row in tickets:
            exit_code = row["exit_code"]
            items.append(
                {
                    "created_at": int(row["created_at"]),
                    "seq": int(row["rid"]),
                    "kind": "ticket",
                    "tool": str(row["tool_name"]),
                    "exit": "-" if exit_code is None else exit_code,
                    "text": str(row["summary"]),
                }
            )
        items.sort(key=lambda item: (item["created_at"], item["kind"] != "say", item["seq"]))
        return render_template_string(_CHAT_TPL, task=task, items=items)

    @app.get("/task/<task_id>/stream")
    def stream(task_id: str) -> Response:
        conn = _open_and_guard(path, task_id)
        assert conn is not None
        return Response(
            stream_with_context(_stream_gen(conn, task_id)),
            mimetype="text/event-stream",
        )

    return app


def _graph_note(nodes: list[sqlite3.Row], edges: list[dict[str, Any]]) -> str:
    """与 store/graph.graph_summary 同口径的只读摘要文案（面板不 import 写口模块）。"""
    kinds: dict[str, int] = {}
    for r in nodes:
        kinds[str(r["kind"])] = kinds.get(str(r["kind"]), 0) + 1
    if not nodes and not edges:
        return ""
    parts = "、".join(f"{k}={n}" for k, n in sorted(kinds.items()))
    return f"节点 {len(nodes)}（{parts}）；边 {len(edges)} 条"


def _stream_gen(conn: sqlite3.Connection, task_id: str) -> Iterator[str]:
    """SSE 生成器：先吐当前 audit 快照，之后每 _STREAM_INTERVAL 秒轮询增量。

    数据帧格式：``data: <JSON 数组>``（audit 的 rowid/actor/what/created_at）。
    """
    last_rowid = 0
    snapshot = [
        dict(r)
        for r in conn.execute(
            "SELECT rowid AS rid, actor, what, created_at FROM audit_log WHERE task_id = ?"
            " ORDER BY rid ASC LIMIT 200",
            (task_id,),
        ).fetchall()
    ]
    if snapshot:
        last_rowid = int(snapshot[-1]["rid"])
    yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
    while True:
        time.sleep(_STREAM_INTERVAL)
        fresh = [
            dict(r)
            for r in conn.execute(
                "SELECT rowid AS rid, actor, what, created_at FROM audit_log WHERE task_id = ?"
                " AND rowid > ? ORDER BY rowid ASC",
                (task_id, last_rowid),
            ).fetchall()
        ]
        if fresh:
            last_rowid = int(fresh[-1]["rid"])
            yield f"data: {json.dumps(fresh, ensure_ascii=False)}\n\n"
        else:
            yield ": ping\n\n"
