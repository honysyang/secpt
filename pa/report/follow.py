"""只读跟随渲染（Part 3）：secpt watch / run --follow。

- render_line(event, step=0, dur_ms=None) -> str：纯函数，逐字节确定，
  trace 可复用；行格式 HH:MM:SS step NNN │ task │ phase │ summary │
  result │ ev-id │ dur（本地时间）；
- watch(root)：按字节偏移 tail events.jsonl（不整读、不回写、零事件），
  缺文件时等待其出现（run --follow 下 bootstrap 稍后建目录）；底部状态
  行每 20 行或 mission.finished 时刷新；遇 mission.finished 打印终止
  原因与报告路径后返回退出码（同 run：0/2/3/4）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

from pa.errors import SecptError
from pa.models import Event

_EXIT = {"completed": 0, "budget_steps": 2, "budget_tokens": 2,
         "plateau": 3, "deadline": 4, "interrupted": 4}

_PHASE = {
    "mission.created": "boot", "plan.created": "plan",
    "plan.revised": "plan", "task.created": "plan",
    "task.dispatched": "dispatch", "task.state_changed": "task",
    "action.started": "action", "action.blocked": "action",
    "action.finished": "action", "evidence.stored": "evidence",
    "clue.added": "clue", "worker.returned": "worker",
    "finding.proposed": "finding", "finding.adjudicated": "finding",
    "budget.charged": "settle", "context.assembled": "context",
    "mentor.advisory": "mentor", "mentor.advisory_agg": "mentor",
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
        return f"工具 {p.get('tool', p.get('fp', '?'))}", \
            f"exit={p.get('exit', '?')}{cached}"
    if t == "evidence.stored":
        return "证据落盘", str(p.get("id", "?"))[:12]
    if t == "clue.added":
        c = p.get("clue", {})
        return _cut(f"线索 {c.get('kind', '?')}:{c.get('key', '?')}", 28), \
            _cut(c.get("value", ""), 10)
    if t == "worker.returned":
        f = p.get("failure")
        return "worker 返回", f"fail={f.get('cls')}" if f else "ok"
    if t == "budget.charged":
        return f"结算 tokens={p.get('tokens', 0)}", "-"
    if t == "mentor.advisory":
        return _cut(f"[{p.get('level', '?')}] {p.get('issue', '?')}", 28), \
            str(p.get("target", "-"))
    if t == "mentor.advisory_agg":
        return f"{p.get('issue', '?')} +{p.get('delta', 1)}", \
            str(p.get("target", "-"))
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
    return (f"{ts} step {step:03d} │ {_cut(task, 10):<10} │ "
            f"{phase:<8} │ {summary:<28} │ {result:<12} │ "
            f"#{ev.seq:<5} │ {dur}")


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
        failed = sum(1 for s in self.task_states.values()
                     if s in ("failed", "superseded"))
        return (f"steps={self.steps} tokens={self.tokens // 1000}k "
                f"tasks=done {done} failed {failed} clues {self.clues} "
                f"findings {self.findings} mentor={mentor}")


def watch(root, *, out=None, status_every: int = 20, poll: float = 0.2,
          stop: threading.Event | None = None,
          wait: float = 30.0) -> int:
    """tail events.jsonl 并渲染。遇 mission.finished 打印终止原因与报告
    路径后返回退出码；Ctrl-C 由调用方捕获（返回 130）。只读：不打开
    任何文件写入，不产生事件。"""
    from pa.policy.coach import mode as mentor_mode

    root = Path(root)
    out = out if out is not None else sys.stdout
    path = root / "events.jsonl"
    deadline = time.monotonic() + wait
    while not path.exists():
        if stop is not None and stop.is_set():
            return 130
        if time.monotonic() > deadline:
            raise SecptError(f"等待事件账本超时: {path}")
        time.sleep(poll)

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
                started = dispatched_at.pop(
                    str(ev.payload.get("task_id", "")), None)
                if started is not None:
                    dur_ms = int((ev.ts - started).total_seconds() * 1000)
            emit(render_line(ev, step=step, dur_ms=dur_ms) + "\n")
            counters.feed(ev)
            rendered += 1
            if rendered % status_every == 0:
                emit("── " + counters.status(mentor_mode()) + " ──\n")
            if ev.type == "mission.finished":
                finished = ev.payload
        flush()
        if finished is not None:
            reason = str(finished.get("reason", "error"))
            emit(f"终止：{reason}（{finished.get('summary', '')}）\n")
            emit(f"报告：{(root / 'report.md').resolve()}\n")
            return _EXIT.get(reason, 1)
        time.sleep(poll)
