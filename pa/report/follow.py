"""只读跟随渲染（Part 3）：secpt watch / run --follow。

- render_line(event, step=0, dur_ms=None) -> str：纯函数，逐字节确定，
  trace 可复用；行格式 HH:MM:SS step NNN │ task │ phase │ summary │
  result │ ev-id │ dur（本地时间）；
- watch(root, verbose=False, flow=False)：按字节偏移 tail events.jsonl
  （不整读、不回写、零事件），缺文件时等待其出现（run --follow 下
  bootstrap 稍后建目录）。三种口径：flow=True 用 pa.report.flow.Flow
  渲染人类可读流程叙事（secpt do 默认）；verbose=True 渲染每个事件的
  全 payload 细节（do --raw）；默认单行摘要（run --follow / watch）。
  遇 mission.finished 打印终止原因与报告路径后返回退出码（0/2/3/4）。
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pa.errors import SecptError
from pa.models import Event

_EXIT = {
    "completed": 0,
    "budget_steps": 2,
    "budget_tokens": 2,
    "plateau": 3,
    "deadline": 4,
    "interrupted": 4,
    "failed": 1,
}

_PHASE = {
    "mission.created": "boot",
    "plan.created": "plan",
    "plan.revised": "plan",
    "task.created": "plan",
    "decision.made": "decide",
    "task.dispatched": "dispatch",
    "task.state_changed": "task",
    "action.started": "action",
    "action.blocked": "action",
    "action.finished": "action",
    "evidence.stored": "evidence",
    "clue.added": "clue",
    "worker.returned": "worker",
    "finding.proposed": "finding",
    "finding.adjudicated": "finding",
    "budget.charged": "settle",
    "budget.warning": "settle",
    "context.assembled": "context",
    "mentor.advisory": "mentor",
    "mentor.advisory_agg": "mentor",
    "mentor.intervened": "mentor",
    "restart.decided": "restart",
    "memory.reflect": "memory",
    "model.switched": "model",
    "mission.audit": "audit",
    "mission.finished": "finish",
}


def _cut(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _summarize(ev: Event) -> tuple[str, str]:
    """(summary, result)：只读 payload 真实字段，不猜。"""
    p = ev.payload
    t = ev.type
    if t == "mission.created":
        return _cut("目标 " + ", ".join(p.get("targets", [])), 28), "-"
    if t == "plan.created":
        return f"任务草案 n={p.get('n', '?')}", "-"
    if t == "task.created":
        return _cut(p.get("task", {}).get("goal", "task"), 28), "-"
    if t == "decision.made":
        kind = str(p.get("kind") or "")
        if kind == "orchestrate":
            return (
                f"编排 派发{len(p.get('run') or [])}/"
                f"新建{len(p.get('created') or [])}",
                f"mode={p.get('mode', '?')}",
            )
        if kind == "finish":
            return "思考 收口", _cut(p.get("summary", ""), 12)
        if kind == "batch":
            return f"思考 批量 {p.get('count', '?')}", "-"
        if kind == "model_error":
            return "思考 模型失败", str(p.get("error_type", "?"))
        return f"思考 {kind or 'decision'}", "-"
    if t == "task.dispatched":
        return "派发", "-"
    if t == "task.state_changed":
        return "状态迁移", f"{p.get('from', '?')}→{p.get('to', '?')}"
    if t == "action.started":
        return f"工具 {p.get('tool', '?')}", "start"
    if t == "action.blocked":
        return f"工具 {p.get('tool', '?')} 被拦", str(p.get("reason", "?"))
    if t == "action.finished":
        cached = " cached" if p.get("cached") else ""
        return (
            f"工具 {p.get('tool', p.get('fp', '?'))}",
            f"exit={p.get('exit', '?')}{cached}",
        )
    if t == "evidence.stored":
        return "证据落盘", str(p.get("id", "?"))[:12]
    if t == "clue.added":
        c = p.get("clue", {})
        return _cut(f"线索 {c.get('kind', '?')}:{c.get('key', '?')}", 28), _cut(
            c.get("value", ""), 10
        )
    if t == "worker.returned":
        f = p.get("failure")
        return "worker 返回", f"fail={f.get('cls')}" if f else "ok"
    if t == "budget.charged":
        return f"结算 tokens={p.get('tokens', 0)}", "-"
    if t == "mentor.advisory":
        return _cut(f"[{p.get('level', '?')}] {p.get('issue', '?')}", 28), str(
            p.get("target", "-")
        )
    if t == "mentor.advisory_agg":
        return f"{p.get('issue', '?')} +{p.get('delta', 1)}", str(p.get("target", "-"))
    if t == "mission.finished":
        return _cut(p.get("summary", ""), 28), str(p.get("reason", "?"))
    if t == "context.assembled":
        return "上下文装配", "-"
    return _cut(t, 28), "-"


def render_line(ev: Event, step: int = 0, dur_ms: int | None = None) -> str:
    """单事件 → 一行。纯函数：同事件同参数 ⇒ 逐字节相同。"""
    ts = ev.ts.astimezone().strftime("%H:%M:%S")
    task = str(ev.payload.get("task_id") or ev.payload.get("target") or "-")
    phase = _PHASE.get(ev.type, ev.type.split(".")[0])
    summary, result = _summarize(ev)
    dur = f"{dur_ms}ms" if dur_ms is not None else "-"
    return (
        f"{ts} step {step:03d} │ {_cut(task, 10):<10} │ "
        f"{phase:<8} │ {summary:<28} │ {result:<12} │ "
        f"#{ev.seq:<5} │ {dur}"
    )


class _Counters:
    """状态行所需的账本口径计数（只从已见事件累计，可重放）。"""

    def __init__(self) -> None:
        self.steps = 0
        self.tokens = 0
        self.clues = 0
        self.findings = 0
        self.task_states: dict[str, str] = {}

    def feed(self, ev: Event) -> None:
        p = ev.payload
        if ev.type == "budget.charged":
            self.steps += 1
            self.tokens += int(p.get("tokens", 0))
        elif ev.type == "clue.added":
            self.clues += 1
        elif ev.type == "finding.proposed":
            self.findings += 1
        elif ev.type == "task.state_changed":
            self.task_states[str(p.get("task_id", ""))] = str(p.get("to", ""))

    def status(self, mentor: str) -> str:
        done = sum(1 for s in self.task_states.values() if s == "done")
        failed = sum(
            1 for s in self.task_states.values() if s in ("failed", "superseded")
        )
        return (
            f"steps={self.steps} tokens={self.tokens // 1000}k "
            f"tasks=done {done} failed {failed} clues {self.clues} "
            f"findings {self.findings} mentor={mentor}"
        )


def watch(
    root,
    *,
    out=None,
    status_every: int = 20,
    poll: float = 0.2,
    stop: threading.Event | None = None,
    wait: float = 30.0,
    verbose: bool = False,
    flow: bool = False,
) -> int:
    """tail events.jsonl 并渲染。遇 mission.finished 打印终止原因与报告
    路径后返回退出码；Ctrl-C 由调用方捕获（返回 130）。只读：不打开
    任何文件写入，不产生事件。

    三种口径：
    - flow=True：人类可读流程叙事（planner → orchestrator → worker →
      裁决 → 收口），do 默认；
    - verbose=True：全 payload 详情块（tui.detail_lines 同口径），
      `do --raw` 使用；
    - 默认 False：单行摘要（run --follow / watch）。
    """
    from pa.policy.coach import mode as mentor_mode

    root = Path(root)
    out = out if out is not None else sys.stdout
    path = root / "events.jsonl"
    width = max(48, shutil.get_terminal_size().columns - 2)
    flow_renderer = None
    detail_of = None
    if not flow and verbose:
        # 惰性导入：单行模式不背 curses 依赖（tui 顶层 import curses）
        from pa.report.tui import detail_lines

        detail_of = detail_lines
    deadline = time.monotonic() + wait
    while not path.exists():
        if stop is not None and stop.is_set():
            return 130
        if time.monotonic() > deadline:
            raise SecptError(f"等待事件账本超时: {path}")
        time.sleep(poll)
    if flow:
        # 必须等 events.jsonl 出现后再构造：run() 线程先写 mission.json
        # 再写 mission.created；提前构造会读到空目录，头部显示「目标 ?」。
        from pa.report.flow import Flow

        flow_renderer = Flow(
            root,
            out=out,
            color=bool(getattr(out, "isatty", lambda: False)()),
        )

    emit = out.write
    flush = getattr(out, "flush", lambda: None)
    counters = _Counters()
    dispatched_at: dict[str, Any] = {}
    offset = 0
    buf = ""
    rendered = 0
    step = 0
    finished: dict[str, Any] | None = None
    while True:
        if stop is not None and stop.is_set():
            return 130
        with path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            data = f.read()
            offset = f.tell()
        buf += data
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            ev = Event.model_validate_json(line)
            if ev.type == "budget.charged":
                step += 1
            dur_ms = None
            if ev.type == "task.dispatched":
                dispatched_at[str(ev.payload.get("task_id", ""))] = ev.ts
            elif ev.type == "worker.returned":
                started = dispatched_at.pop(str(ev.payload.get("task_id", "")), None)
                if started is not None:
                    dur_ms = int((ev.ts - started).total_seconds() * 1000)
            if flow_renderer is not None:
                flow_renderer.feed(ev)
            elif detail_of is not None:
                # 全量详情块：header（seq/type/actor/ts）+ payload 全字段
                # + 证据全文预览（截断上限同 TUI）；空行分块便于扫读
                emit("\n".join(detail_of(ev, root, width)) + "\n")
                emit("\n")
            else:
                emit(render_line(ev, step=step, dur_ms=dur_ms) + "\n")
            counters.feed(ev)
            rendered += 1
            if flow_renderer is None and rendered % status_every == 0:
                emit("── " + counters.status(mentor_mode()) + " ──\n")
            if ev.type == "mission.finished":
                finished = ev.payload
        flush()
        if finished is not None:
            reason = str(finished.get("reason", "error"))
            if flow_renderer is None:
                emit(f"终止：{reason}（{finished.get('summary', '')}）\n")
                # 报告统一落家族根（save_report 家族级聚合），用户不需要记
                # runs/N 的具体编号；旧裸 hash 账本（root 即家族根）原样打印
                if root.parent.name == "runs":
                    emit(f"报告：{(root.parent.parent / 'report.md').resolve()}\n")
                else:
                    emit(f"报告：{(root / 'report.md').resolve()}\n")
            return _EXIT.get(reason, 1)
        time.sleep(poll)
