"""CLI 可观测（§8 M1 面）：角色气泡 + 小票摘要行 + evidence 落地检查窗。

为什么是订阅者而不是散落 print：§8 钉死"事件流全部由 store/loop 触发，ob/*
只读订阅"——本模块只吃 EventBus 事件与账本只读 SELECT，绝不反向发明事件
语义、绝不写账（append-only 红线管的是写，检查窗全部只读）。

双通道纪律：
- 对话面板（角色气泡/小票摘要行）走 stdout，人读；
- 结构化日志（structlog JSON）走 stderr，机器读——两个通道不互污，M2 web
  面板可以直接复用 stderr 之上的事件流契约（:class:`CliView` 同一订阅接口）。
  日志装配（级别/文件双写/行缓冲）在 :mod:`secpt.ob.logging_setup`，此处再导出
  ``configure_logging`` 保持旧 import 面不破。
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from typing import TextIO

from secpt.errors import LedgerError
from secpt.ob.logging_setup import configure_logging

# 审计尾行展示量：检查窗是"够解决问题的最小回放"，不是全库 dump。
_AUDIT_TAIL = 10


def _fmt_ms(raw: object) -> str:
    """耗时人类化（M5-29）：毫秒 → ``0.8s`` / ``12.3s`` / ``1m05s``。

    为什么在 CLI 层格式化而不是事件里带字符串：事件载荷保持纯数字（面板/日志
    都能再加工），显示口径只有这一处，改样式不必动事件契约。
    """
    try:
        ms = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if ms < 0:
        return ""
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    seconds = ms // 1000
    return f"{seconds // 60}m{seconds % 60:02d}s"


class CliView:
    """EventBus 订阅者：把 M1 三事件渲染成对话面板（角色气泡 + 小票摘要行）。

    载荷逐键 ``get`` 缺省兜底：可观测是旁观者，载荷缺键也绝不能把渲染炸掉
    （总线层还会兜一层异常隔离，双保险）。
    """

    def __init__(self, out: TextIO | None = None) -> None:
        """``out`` 仅供测试注入；默认写 :data:`sys.stdout`（动态取，兼容 capsys）。

        ``_pending_tickets`` 是**显示层缓冲**（M5-26）：事件顺序是"先落票、后广播
        图变化"，但人读日志的自然顺序是"先看这步想干什么、再看干了什么"。小票行
        先暂存，等本步决策行出现时一起冲刷——只改输出顺序，不改事件语义。
        """
        self._out = out
        self._pending_tickets: list[str] = []
        # M5-29：worker 线程也往同一个面板写（agent_run 收拢窗内并发派单），
        # 行级锁保证不撕裂；print 本身是单次 write，锁只是把"整行"钉死。
        self._lock = threading.Lock()

    def _print(self, line: str) -> None:
        with self._lock:
            print(line, file=self._out or sys.stdout)

    def _flush_tickets(self) -> None:
        for line in self._pending_tickets:
            self._print(line)
        self._pending_tickets.clear()

    def __call__(self, event: str, payload: Mapping[str, object]) -> None:
        handler = _HANDLERS.get(event)
        if handler is not None:
            handler(self, payload)

    def _on_step_started(self, payload: Mapping[str, object]) -> None:
        self._flush_tickets()  # 兜底：上一步缺 graph_changed 时也别把回执吞掉
        self._print(
            f"【主LLM】第 {payload.get('step_no', '?')}/{payload.get('max_steps', '?')} 步"
            " · 开始决策"
        )

    def _on_ticket(self, payload: Mapping[str, object]) -> None:
        """工具回执行（§8"一行 summary"）：标明是哪个智能体调了哪个工具。"""
        cost = _fmt_ms(payload.get("duration_ms"))
        tail = f"（{cost}）" if cost else ""
        self._pending_tickets.append(
            f"【主LLM → {payload.get('tool_name', '?')}】exit={payload.get('exit_code')}"
            f"{tail} | {payload.get('summary', '')}"
        )

    def _on_run_ended(self, payload: Mapping[str, object]) -> None:
        self._flush_tickets()
        cost = _fmt_ms(payload.get("duration_ms"))
        tail = f" 用时={cost}" if cost else ""
        # M5-31 时长分解（有则带出）：慢在决策还是慢在工具，收尾一行可辨。
        split = _fmt_ms(payload.get("llm_ms")), _fmt_ms(payload.get("tools_ms"))
        if any(split):
            tail += f"（决策 {split[0] or '-'} · 工具 {split[1] or '-'}）"
        findings = payload.get("findings")
        found = f" findings={findings}" if findings is not None else ""
        self._print(
            f"【出口】exit_kind={payload.get('exit_kind', '?')} steps={payload.get('steps', '?')}"
            f"{found}{tail} task_id={payload.get('task_id', '?')}"
        )

    def _on_graph_changed(self, payload: Mapping[str, object]) -> None:
        """本步决策行（M5-26）：先讲"想了什么/用了哪些工具"，再放工具回执。

        事件名是 graph_changed（写图后广播），但载荷里带 thought/tools——CLI 借它
        把决策过程上屏；EventBus 只报告不接管，多带字段不改变任何执行语义。
        M5-31：决策与工具拆开计时（decision_ms/tools_ms）——"决策 5s · 工具 2m00s"
        一眼看清慢在模型还是慢在扫描；旧装配没带拆分字段时回落整步耗时。
        """
        tools = str(payload.get("tools") or "")
        tail = f"（工具：{tools}）" if tools else "（无工具）"
        decision = _fmt_ms(payload.get("decision_ms"))
        dispatch = _fmt_ms(payload.get("tools_ms"))
        if decision or dispatch:
            parts = [
                part
                for part in (
                    f"决策 {decision}" if decision else "",
                    f"工具 {dispatch}" if dispatch else "",
                )
                if part
            ]
            spent = f" · {' · '.join(parts)}"
        else:
            cost = _fmt_ms(payload.get("duration_ms"))
            spent = f" · {cost}" if cost else ""
        self._print(
            f"【主LLM】第 {payload.get('step_no', '?')} 步决策{spent}："
            f"{payload.get('thought', '')}{tail}"
        )
        self._flush_tickets()  # 决策在前、回执在后：人读日志的自然顺序
        self._print(f"【双图】+{payload.get('nodes', 0)}n+{payload.get('edges', 0)}e")

    # ---- M5-29 worker 过程可见（多 worker 并发时也能分清是谁在干什么）------------

    def _on_worker_step_started(self, payload: Mapping[str, object]) -> None:
        actor = payload.get("actor", "worker")
        self._print(
            f"【{actor}】第 {payload.get('step_no', '?')}/{payload.get('step_budget', '?')} 步"
            " · 开始"
        )

    def _on_worker_step_ended(self, payload: Mapping[str, object]) -> None:
        actor = payload.get("actor", "worker")
        cost = _fmt_ms(payload.get("duration_ms"))
        spent = f"（{cost}）" if cost else ""
        self._print(
            f"【{actor}】第 {payload.get('step_no', '?')} 步完成{spent}："
            f"{payload.get('thought', '')}"
        )

    def _on_worker_ticket(self, payload: Mapping[str, object]) -> None:
        actor = payload.get("actor", "worker")
        cost = _fmt_ms(payload.get("duration_ms"))
        spent = f"（{cost}）" if cost else ""
        self._print(
            f"【{actor} → {payload.get('tool_name', '?')}】exit={payload.get('exit_code')}"
            f"{spent} | {payload.get('summary', '')}"
        )

    def _on_worker_mission_ended(self, payload: Mapping[str, object]) -> None:
        actor = payload.get("actor", "worker")
        state = "完工" if payload.get("finished") else "未完工"
        cost = _fmt_ms(payload.get("duration_ms"))
        spent = f" 用时={cost}" if cost else ""
        self._print(
            f"【{actor}·收单】{state} steps={payload.get('steps', '?')}"
            f" findings={payload.get('findings', '?')}{spent}"
        )

    def _on_exit_failed(self, payload: Mapping[str, object]) -> None:
        """出场未过（M5-26）：把失败明细讲清楚，别让人只看到一个计数。"""
        failures = payload.get("failures") or []
        detail = "；".join(str(item) for item in failures) if failures else "（无明细）"
        self._print(f"【出场】第 {payload.get('streak', '?')} 轮未过：{detail}")

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
    "exit_failed": CliView._on_exit_failed,
    "run_ended": CliView._on_run_ended,
    "worker_step_started": CliView._on_worker_step_started,
    "worker_step_ended": CliView._on_worker_step_ended,
    "worker_ticket": CliView._on_worker_ticket,
    "worker_mission_ended": CliView._on_worker_mission_ended,
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

    # M5-34 findings 状态机明细：审计窗要看的是"谁申报的、判到哪一步、依据什么
    # 方法、锚在哪"，一行数量加陈旧文案（"M1 无裁判"）什么问题也判断不了。
    findings = conn.execute(
        "SELECT title, detail, verify_state, method, evidence_ref, actor, sweep_count"
        " FROM findings WHERE task_id = ? ORDER BY created_at, rowid",
        (task_id,),
    ).fetchall()
    lines.append(f"-- findings -- {len(findings)} 条（claimed→verified/rejected）--")
    if not findings:
        lines.append("(无申报)")
    for row in findings:
        lines.append(
            f"[{row['verify_state']}|{row['method'] or '-'}|sweep={row['sweep_count']}]"
            f" {row['actor']}: {row['title']} → {row['evidence_ref']}"
        )
        detail = " ".join(str(row["detail"]).split())
        if detail:
            lines.append(f"    detail: {detail[:200]}")

    # M5-34 observations 客观事件流：LLM 小票之外的流量真相，判真伪的第二通道。
    observations = conn.execute(
        "SELECT channel, meta_json, snapshot_ref, ts FROM observations"
        " WHERE task_id = ? ORDER BY ts, rowid",
        (task_id,),
    ).fetchall()
    lines.append(f"-- observations -- {len(observations)} 条（LLM 小票之外的客观流）--")
    if not observations:
        lines.append("(探针未开启或无抓包)")
    for row in observations:
        lines.append(f"[{row['channel']}] ts={row['ts']} → {row['snapshot_ref']}")

    lines.append(f"-- audit（尾 {_AUDIT_TAIL} 行，新→旧）--")
    audits = conn.execute(
        "SELECT actor, what FROM audit_log WHERE task_id = ? ORDER BY id DESC LIMIT ?",
        (task_id, _AUDIT_TAIL),
    ).fetchall()
    for row in reversed(audits):
        lines.append(f"{row['actor']}: {row['what']}")
    return lines


__all__ = ["CliView", "configure_logging", "evidence_report"]
