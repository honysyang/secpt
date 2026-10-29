"""CLI 可观测（§8 M1 面）：角色气泡 + 小票摘要行 + evidence 落地检查窗。

为什么是订阅者而不是散落 print：§8 钉死"事件流全部由 store/loop 触发，ob/*
只读订阅"——本模块只吃 EventBus 事件与账本只读 SELECT，绝不反向发明事件
语义、绝不写账（append-only 红线管的是写，检查窗全部只读）。

双通道纪律：
- 对话面板（角色气泡/小票摘要行）走 stdout，人读；
- 结构化日志（structlog JSON）走 stderr，机器读——两个通道不互污，M2 web
  面板可以直接复用 stderr 之上的事件流契约（:class:`CliView` 同一订阅接口）。
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable, Mapping
from typing import TextIO

import structlog

from secpt.errors import LedgerError

# 审计尾行展示量：检查窗是"够解决问题的最小回放"，不是全库 dump。
_AUDIT_TAIL = 10


class _StderrFactory:
    """每次实例化 logger 都现取 :data:`sys.stderr`。

    为什么不用 ``PrintLoggerFactory(file=sys.stderr)``：后者在 configure 时捕获
    stderr 的对象引用，而 pytest 等环境会整块替换 ``sys.stderr``，旧引用一旦
    闭合再写入就抛 ``ValueError: I/O operation on closed file``。动态解析把
    "哪个流是 stderr" 的裁决权推迟到每次写日志的那一刻。
    """

    def __call__(self, *args: object) -> structlog.PrintLogger:
        return structlog.PrintLogger(file=sys.stderr)


def configure_logging() -> None:
    """structlog 全量 JSON 落 stderr（§8：结构化日志全步数进频道，机器读）。"""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=_StderrFactory(),
    )


class CliView:
    """EventBus 订阅者：把 M1 三事件渲染成对话面板（角色气泡 + 小票摘要行）。

    载荷逐键 ``get`` 缺省兜底：可观测是旁观者，载荷缺键也绝不能把渲染炸掉
    （总线层还会兜一层异常隔离，双保险）。
    """

    def __init__(self, out: TextIO | None = None) -> None:
        """``out`` 仅供测试注入；默认写 :data:`sys.stdout`（动态取，兼容 capsys）。"""
        self._out = out

    def _print(self, line: str) -> None:
        print(line, file=self._out or sys.stdout)

    def __call__(self, event: str, payload: Mapping[str, object]) -> None:
        handler = _HANDLERS.get(event)
        if handler is not None:
            handler(self, payload)

    def _on_step_started(self, payload: Mapping[str, object]) -> None:
        self._print(f"【主LLM】step {payload.get('step_no', '?')} 开包")

    def _on_ticket(self, payload: Mapping[str, object]) -> None:
        """小票摘要行（§8"一行 summary"）：标明是哪个智能体调了哪个工具。"""
        self._print(
            f"【主LLM → {payload.get('tool_name', '?')}】exit={payload.get('exit_code')}"
            f" | {payload.get('summary', '')}"
        )

    def _on_run_ended(self, payload: Mapping[str, object]) -> None:
        self._print(
            f"【出口】exit_kind={payload.get('exit_kind', '?')} steps={payload.get('steps', '?')}"
            f" task_id={payload.get('task_id', '?')}"
        )

    def _on_graph_changed(self, payload: Mapping[str, object]) -> None:
        """M4-6 图变化行（事件线只报告不接管）：双图每步落账后广播写痕。"""
        self._print(
            f"【双图】step {payload.get('step_no', '?')} 更新"
            f" +{payload.get('nodes', 0)}n+{payload.get('edges', 0)}e"
        )

    def _on_plan_reviewed(self, payload: Mapping[str, object]) -> None:
        """M5 规划重估行（角色气泡：LLMC）：提案经下一包吸收，本行只报告不裁决。"""
        self._print(
            f"【LLMC·教练】step {payload.get('step_no', '?')} 规划重估：{payload.get('idea', '')}"
        )


def _on_unknown(_view: CliView, _payload: Mapping[str, object]) -> None:
    """M1 契约外的 M2 事件不渲染——面板保持干净，接口在、接线留给后续里程碑。"""


_HANDLERS: dict[str, Callable[[CliView, Mapping[str, object]], None]] = {
    "run_step_started": CliView._on_step_started,
    "ticket_appended": CliView._on_ticket,
    "graph_changed": CliView._on_graph_changed,
    "plan_reviewed": CliView._on_plan_reviewed,
    "run_ended": CliView._on_run_ended,
}


def evidence_report(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """``secpt evidence`` 检查窗：按表落一份只读快照（§6.3 运行回放的 M1 形态）。

    覆盖 tasks/runs（水位线）/steps/小票/findings/audit 尾行六段，小票带 raw_ref
    ——检查窗的职责就是让锚点可核（点开 raw_ref 应见真文件）。
    查无此 task 显式抛 :class:`LedgerError`，由 main.py 兜底转非零出口。
    """
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise LedgerError("evidence: tasks 查无此行", context={"task_id": task_id})

    lines = ["== evidence · task =="]
    goal = task["goal"] if task["goal"] is not None else "(对话模式/未固化)"
    lines.append(
        f"task_id={task['id']} mode={task['mode']} goal={goal} exit_kind={task['exit_kind']}"
    )

    lines.append("-- runs --")
    runs = conn.execute(
        "SELECT id, status, step_count, started_at, ended_at FROM runs"
        " WHERE task_id = ? ORDER BY started_at, rowid",
        (task_id,),
    ).fetchall()
    for run in runs:
        lines.append(
            f"run {run['id']} status={run['status']} step_count={run['step_count']}"
            f" started={run['started_at']} ended={run['ended_at']}"
        )

    lines.append("-- steps --")
    for step in conn.execute(
        "SELECT run_id, step_no, status FROM steps WHERE task_id = ? ORDER BY rowid",
        (task_id,),
    ).fetchall():
        lines.append(f"step {step['step_no']} status={step['status']} run={step['run_id']}")

    lines.append("-- plans --")
    plans = conn.execute(
        "SELECT version, step_no, status, content FROM plans WHERE task_id = ? ORDER BY rowid",
        (task_id,),
    ).fetchall()
    if not plans:
        lines.append("(尚无规划)")
    for row in plans:
        lines.append(f"v{row['version']} step={row['step_no']} [{row['status']}] {row['content']}")

    lines.append("-- 小票（含锚点 raw_ref）--")
    tickets = conn.execute(
        "SELECT tool_name, exit_code, summary, raw_ref FROM tickets"
        " WHERE task_id = ? ORDER BY created_at, rowid",
        (task_id,),
    ).fetchall()
    if not tickets:
        lines.append("(尚无小票)")
    for tick in tickets:
        lines.append(
            f"{tick['tool_name']} | exit={tick['exit_code']} | {tick['summary']}"
            f" → {tick['raw_ref']}"
        )

    findings = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    lines.append(f"-- findings -- {findings} 条（M1 无裁判，findings 留待 M2）")

    lines.append(f"-- audit（尾 {_AUDIT_TAIL} 行，新→旧）--")
    audits = conn.execute(
        "SELECT actor, what FROM audit_log WHERE task_id = ? ORDER BY id DESC LIMIT ?",
        (task_id, _AUDIT_TAIL),
    ).fetchall()
    for row in reversed(audits):
        lines.append(f"{row['actor']}: {row['what']}")
    return lines


__all__ = ["CliView", "configure_logging", "evidence_report"]
