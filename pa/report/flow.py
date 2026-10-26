"""人类可读的 mission 流程叙事（secpt do 默认 / run --follow --flow）。

把 append-only 事件账本翻译成「人读得懂」的四层叙事，而不是把 payload
平铺出来：

1. 引导：planner 为什么拆这些任务；
2. 每步：orchestrator 为什么派发/新建，策略是什么；
3. 每个 worker：思考（预期/理由/收口/批量/失败）→ 行动 → 观察 → 线索
   → 判定；
4. 旁路：教练、JEV、记忆简报、出口审计、收口。

只读：不产生事件、不写账本。流式模式（watch/do）允许时间戳与颜色；
离线模式（log）不带时间戳，保证同一账本两次渲染逐字节一致。

设计取舍：这里不追求「全量字段」——全量 payload 留给 tui / trace /
`do --raw`。流程日志回答的是「谁在什么时候、基于什么、做了什么决定」，
不是替代审计账本。
"""

from __future__ import annotations

import json
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from pa.core.evidence_kinds import acceptance_hit
from pa.infra import evidence as evidence_store
from pa.infra.log import get_logger
from pa.models import Clue, Event, MissionSpec
from pa.store.view import StoreView

logger = get_logger("secpt.flow")

_COLOR = {
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "bold": "\033[1m",
}
_RESET = "\033[0m"

_SECTION_NAMES = {
    "system": "系统",
    "brief": "简报",
    "strategy": "策略",
    "skills": "技能",
    "role": "角色",
    "role_catalog": "角色目录",
    "task": "任务",
    "criteria": "判据",
    "clues": "线索",
    "lessons": "经验",
    "advisories": "教练建议",
    "round_brief": "上轮简报",
    "untrusted": "不可信输出",
    "tail": "尾部指令",
}

_PHASE_NAMES = {
    "planner": "planner",
    "orchestrator": "orchestrator",
    "worker": "worker",
    "core": "core",
}


def _cut(value: Any, n: int) -> str:
    s = str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_seconds(n: int) -> str:
    n = int(n or 0)
    if n >= 60:
        return f"{n // 60}m{n % 60:02d}s"
    return f"{n}s"


def _load_spec(root: Path) -> MissionSpec | None:
    p = root / "mission.json"
    try:
        return MissionSpec.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_raw_mission(root: Path) -> dict:
    p = root / "mission.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _family_name(root: Path) -> str:
    """runs/N 账本用家族名（do-<hash8>）展示，不用 N。"""
    root = Path(root)
    if root.parent.name == "runs":
        return root.parent.parent.name
    return root.name


def _report_path(root: Path) -> Path:
    root = Path(root)
    base = root.parent.parent if root.parent.name == "runs" else root
    return (base / "report.md").resolve()


class Flow:
    """事件 → 流程叙事。既可流式 feed，也可 render_all 离线渲染。"""

    def __init__(
        self,
        root,
        *,
        out=None,
        color: bool = False,
        width: int | None = None,
    ) -> None:
        self.root = Path(root)
        self.out = out
        self.color = color
        self.width = width or self._terminal_width()
        self.spec = _load_spec(self.root)
        self.raw_mission = _load_raw_mission(self.root)
        self.lines: list[str] = []
        self._header = False
        self._finished = False
        self._last_blank = False
        self._step = 0
        self._spent_tokens = 0
        self._first_ts: datetime | None = None
        self._last_ts: datetime | None = None
        self.tasks: dict[str, dict] = {}
        self.task_states: dict[str, str] = {}
        self.clues: list[Clue] = []
        self.proposed: list[dict] = []
        self.adjudicated: list[dict] = []
        self._pending: list[dict] = []
        self._worker: str | None = None
        self._worker_open = False
        self._worker_attempt = 0
        self._actions: dict[tuple[str, str], dict] = {}
        self._context_seen: set[tuple] = set()

    # ── 输出原语 ──────────────────────────────────────────────

    def _terminal_width(self) -> int:
        try:
            import shutil

            return max(72, min(120, shutil.get_terminal_size().columns - 2))
        except Exception:
            return 96

    def _style(self, text: str, style: str | None) -> str:
        if not self.color or not style:
            return text
        code = _COLOR.get(style)
        if not code:
            return text
        return "\n".join(code + line + _RESET for line in str(text).splitlines())

    def _emit(self, text: str = "", *, style: str | None = None) -> None:
        text = self._style(str(text), style)
        if self.out is not None:
            self.out.write(text + "\n")
            flush = getattr(self.out, "flush", None)
            if flush is not None:
                flush()
        else:
            self.lines.append(text)
        self._last_blank = text == ""

    def _blank(self) -> None:
        if not self._last_blank:
            self._emit("")

    def _emit_wrapped(
        self,
        text: Any,
        *,
        prefix: str = "",
        cont: str | None = None,
        style: str | None = None,
    ) -> None:
        raw = str(text)
        cont = " " * len(prefix) if cont is None else cont
        width = max(24, self.width - len(prefix))
        for raw_line in raw.splitlines() or [""]:
            chunks = textwrap.wrap(
                raw_line,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]
            for i, chunk in enumerate(chunks):
                self._emit((prefix if i == 0 else cont) + chunk, style=style)

    def _kv(self, label: str, value: Any, *, indent: int = 2) -> None:
        prefix = " " * indent + f"{label:<4} "
        self._emit_wrapped(value, prefix=prefix, cont=" " * len(prefix))

    def _wline(self, text: Any, *, style: str | None = None) -> None:
        if self._worker_open:
            self._emit_wrapped(text, prefix="  │ ", cont="  │   ", style=style)
        else:
            self._emit_wrapped(text, prefix="  ", cont="    ", style=style)

    def _wv(self, label: str, value: Any) -> None:
        prefix = ("  │   " if self._worker_open else "    ") + f"{label:<4} "
        self._emit_wrapped(value, prefix=prefix, cont=" " * len(prefix), style="dim")

    # ── 入口 ──────────────────────────────────────────────────

    def feed(self, ev: Event) -> None:
        """消费一个事件。渲染失败不打断 mission：降级为一行告警。"""
        if not self._header:
            self._print_header()
        if self._first_ts is None:
            self._first_ts = ev.ts
        self._last_ts = ev.ts
        try:
            self._feed(ev)
        except Exception as e:  # 日志渲染绝不能拖垮执行
            logger.warning("流程日志渲染事件 #%s 失败: %s", ev.seq, e)
            self._emit(
                f"[日志] 事件 #{ev.seq} ({ev.type}) 渲染失败："
                f"{type(e).__name__}: {e}",
                style="red",
            )

    def render_all(self, events) -> str:
        """离线渲染：确定性、不带时间戳；只读事件。"""
        for ev in events:
            self.feed(ev)
        if not self._header:
            self._print_header()
        if not self._finished:
            self._blank()
            self._emit("[收口] 未完成（账本里没有 mission.finished）", style="yellow")
        return "\n".join(self.lines).rstrip() + "\n"

    def _feed(self, ev: Event) -> None:
        t, p = ev.type, ev.payload
        if t == "mission.created":
            return
        if t == "task.created":
            task = p.get("task") or {}
            if isinstance(task, dict) and task.get("id"):
                self.tasks[str(task["id"])] = task
                self._pending.append(task)
            return
        if t == "plan.created":
            self._on_plan(p)
        elif t == "plan.revised":
            self._on_plan_revised(p)
        elif t == "decision.made":
            self._on_decision(p)
        elif t == "task.dispatched":
            self._on_dispatch(p)
        elif t == "context.assembled":
            self._on_context(p)
        elif t == "action.started":
            self._on_action_started(ev)
        elif t == "action.blocked":
            self._on_action_blocked(p)
        elif t == "action.finished":
            self._on_action_finished(ev)
        elif t == "clue.added":
            self._on_clue(p)
        elif t == "worker.returned":
            self._on_return(p)
        elif t == "task.state_changed":
            self._on_state(p)
        elif t == "finding.proposed":
            self._on_finding(p)
        elif t == "finding.adjudicated":
            self._on_adjudication(p)
        elif t in ("mentor.advisory", "mentor.advisory_agg"):
            self._on_coach(t, p)
        elif t == "mentor.intervened":
            self._on_intervened(p)
        elif t == "restart.decided":
            self._on_restart(p)
        elif t == "memory.reflect":
            self._on_reflect(p)
        elif t == "budget.charged":
            self._on_budget(p)
        elif t == "budget.warning":
            self._wline(
                f"[预算] 已用 {_fmt_tokens(p.get('spent_tokens', 0))} / "
                f"{_fmt_tokens(p.get('budget_tokens', 0))}",
                style="yellow",
            )
        elif t == "model.switched":
            self._wline(
                f"[模型] 切换 → {p.get('to') or p.get('model') or '?'}",
                style="yellow",
            )
        elif t == "mission.audit":
            self._on_audit(p)
        elif t == "mission.finished":
            self._on_finish(p)
        # evidence.stored 由 action.finished 的观察块承载，不单独刷屏

    # ── 头部 ──────────────────────────────────────────────────

    def _print_header(self) -> None:
        self._header = True
        name = _family_name(self.root)
        self._emit("─" * self.width, style="dim")
        self._emit(f"secpt · {name}", style="cyan")
        spec = self.spec
        raw = self.raw_mission
        objective = spec.objective if spec else str(raw.get("objective") or "?")
        intent = spec.intent if spec else str(raw.get("intent") or "")
        dims = spec.dimensions if spec else list(raw.get("dimensions") or [])
        targets = spec.targets if spec else list(raw.get("targets") or [])
        scope = spec.allowed_scope if spec else list(raw.get("allowed_scope") or [])
        budget = spec.budget if spec else None
        self._kv("目标", objective)
        if intent:
            self._kv("意图", intent)
        if dims:
            self._kv("维度", " / ".join(dims))
        if targets:
            self._kv("清单", "、".join(targets))
        if scope:
            self._kv("范围", "、".join(scope))
        if budget is not None:
            self._kv(
                "预算",
                f"steps≤{budget.steps} · tokens≤{_fmt_tokens(budget.tokens)} · "
                f"seconds≤{budget.seconds} · risk={budget.risk}",
            )
        acceptance = spec.acceptance if spec else list(raw.get("acceptance") or [])
        if acceptance:
            self._emit("  验收：", style="dim")
            for a in acceptance[:8]:
                if hasattr(a, "id"):
                    line = (
                        f"· {a.id}  {a.evidence} @ {a.target}"
                        + (f" {a.path}" if a.path else "")
                        + f" — {a.item}"
                    )
                else:
                    line = (
                        f"· {a.get('id')}  {a.get('evidence')} @ {a.get('target')}"
                        + (f" {a.get('path')}" if a.get("path") else "")
                        + f" — {a.get('item')}"
                    )
                self._emit_wrapped(line, prefix="    ", cont="      ", style="dim")
        files = spec.files if spec else list(raw.get("files") or [])
        if files:
            names = [
                (f.get("name") if isinstance(f, dict) else getattr(f, "name", "?"))
                for f in files
            ]
            self._kv("附件", "、".join(str(n) for n in names[:8]))
        self._blank()

    # ── planner / orchestrator ───────────────────────────────

    def _on_plan(self, p: dict) -> None:
        self._blank()
        n = p.get("n", len(self._pending))
        self._emit(f"[planner] 制定首个计划 · {n} 个任务", style="magenta")
        if p.get("rationale"):
            self._kv("理由", p["rationale"], indent=4)
        strategy = p.get("strategy") or {}
        if strategy:
            self._kv("策略", self._fmt_strategy(strategy), indent=4)
        self._flush_pending("任务草案：")
        self._blank()

    def _on_plan_revised(self, p: dict) -> None:
        ops = p.get("ops") or []
        self._blank()
        self._emit(f"[planner] 修订计划 · {len(ops)} 个操作", style="magenta")
        if p.get("rationale"):
            self._kv("理由", p["rationale"], indent=4)
        self._blank()

    def _on_decision(self, p: dict) -> None:
        kind = str(p.get("kind") or "")
        if kind == "orchestrate":
            self._on_orchestrate(p)
            return
        # worker 的思考事件（worker.py 在决策点追加 decision.made）
        if kind == "finish":
            self._wline("[思考] 决定收口", style="magenta")
            if p.get("summary"):
                self._wv("摘要", p["summary"])
            if p.get("rationale"):
                self._wv("理由", p["rationale"])
            if p.get("satisfied"):
                self._wv("自评满足", "、".join(map(str, p["satisfied"])))
            if p.get("findings_proposed"):
                self._wv("发现提案", f"{p['findings_proposed']} 条进 JEV 裁决")
            elif p.get("findings_skipped"):
                self._wv("发现提案", f"跳过 {p['findings_skipped']} 条（本任务无证据）")
        elif kind == "batch":
            requested = int(p.get("requested") or p.get("count") or 0)
            count = int(p.get("count") or 0)
            extra = f"（请求 {requested}，截断为 {count}）" if requested > count else ""
            self._wline(f"[思考] 批量执行 {count} 个动作{extra}", style="magenta")
            if p.get("rationale"):
                self._wv("理由", p["rationale"])
        elif kind == "model_error":
            self._wline(
                f"[思考] 模型输出不可用 · {p.get('error_type', 'ModelError')}",
                style="red",
            )
            if p.get("detail"):
                self._wv("详情", p["detail"])
        elif kind == "criteria_met":
            self._wline("[思考] 判据已满足，提前收工", style="green")
            if p.get("criteria"):
                self._wv("命中判据", "、".join(map(str, p["criteria"])))
        elif kind == "needs_help":
            self._wline(
                f"[思考] 连续 {p.get('no_progress', '?')} 步没有新信息，"
                "交回编排者",
                style="yellow",
            )
        elif kind == "steps_exhausted":
            self._wline(
                f"[思考] 步数预算耗尽（{p.get('budget_steps', '?')} 步），"
                "以 partial 交回",
                style="yellow",
            )
        elif kind == "blocked":
            self._wline(
                f"[思考] 动作被网关拦截：{p.get('reason', '?')}", style="yellow"
            )
        elif kind == "budget_blocked":
            self._wline("[思考] 任务预算触顶，以 partial 交回", style="yellow")
        elif kind == "empty_batch":
            self._wline("[思考] 批量决策为空，回到反思", style="dim")
        elif p.get("rationale"):
            self._wline(f"[思考] {_cut(p['rationale'], 200)}", style="dim")

    def _on_orchestrate(self, p: dict) -> None:
        self._blank()
        round_no = p.get("round", "?")
        mode = p.get("mode", "?")
        self._emit(
            f"[第 {round_no} 步] orchestrator 编排 · mode={mode}",
            style="cyan",
        )
        if p.get("rationale"):
            self._kv("理由", p["rationale"], indent=4)
        strategy = p.get("current_strategy") or {}
        if strategy:
            self._kv("策略", self._fmt_strategy(strategy), indent=4)
        run = list(p.get("run") or [])
        created = list(p.get("created") or [])
        if run:
            self._emit("  派发：", style="dim")
            for tid in run:
                self._emit_task_line("  ▸ ", str(tid), self.tasks.get(str(tid)))
        else:
            self._emit("  派发：无（本轮只新建/等待）", style="dim")
        if self._pending:
            self._flush_pending("新建：")
        elif created:
            self._emit("  新建：", style="dim")
            for tid in created:
                self._emit_task_line("  + ", str(tid), self.tasks.get(str(tid)))
        abandoned = list(p.get("abandoned") or [])
        if abandoned:
            self._emit("  废止：", style="dim")
            for tid in abandoned:
                self._emit_task_line("  × ", str(tid), self.tasks.get(str(tid)))
        self._blank()

    def _flush_pending(self, heading: str) -> None:
        if not self._pending:
            return
        if heading:
            self._emit("  " + heading, style="dim")
        items = self._pending
        self._pending = []
        for i, task in enumerate(items):
            marker = "  ├ " if i < len(items) - 1 else "  └ "
            self._emit_task_line(marker, str(task.get("id") or "?"), task)

    def _emit_task_line(self, marker: str, tid: str, task: dict | None) -> None:
        task = task or {}
        typ = task.get("type") or "?"
        goal = task.get("goal") or ""
        extras: list[str] = []
        if task.get("role"):
            extras.append(f"角色 {task['role']}")
        if task.get("deps"):
            extras.append("依赖 " + ",".join(map(str, task["deps"])))
        if task.get("allowed_tools"):
            extras.append("工具 " + ",".join(map(str, task["allowed_tools"])))
        budget = task.get("budget") or {}
        if isinstance(budget, dict) and budget.get("steps"):
            extras.append(f"预算 {budget['steps']} 步")
        line = f"{marker}{tid} [{typ}] {goal}"
        if extras:
            line += " · " + " · ".join(extras)
        cont = " " * len(marker)
        self._emit_wrapped(line, prefix="", cont=cont)

    # ── worker 生命周期 ──────────────────────────────────────

    def _on_dispatch(self, p: dict) -> None:
        if self._pending:
            self._flush_pending("任务：")
        tid = str(p.get("task_id") or "?")
        attempt = int(p.get("attempt") or 0)
        task = self.tasks.get(tid) or {}
        if self._worker_open and self._worker != tid:
            self._emit("  └ [未收口] 上一个 worker 没有终态", style="yellow")
            self._worker_open = False
        self._worker = tid
        self._worker_attempt = attempt
        self._worker_open = True
        typ = task.get("type") or "?"
        role = f" · 角色 {task['role']}" if task.get("role") else ""
        budget = task.get("budget") or {}
        steps = f" · 预算 {budget.get('steps')} 步" if isinstance(budget, dict) and budget.get("steps") else ""
        self._blank()
        self._emit(
            f"  ┌ worker {tid} [{typ}] attempt {attempt}{role}{steps}",
            style="cyan",
        )
        if task.get("goal"):
            self._wline(f"目标: {task['goal']}")

    def _on_context(self, p: dict) -> None:
        phase = str(p.get("phase") or "?")
        step = int(p.get("step") or 0)
        attempt = int(p.get("attempt") or 0)
        task_id = str(p.get("task_id") or "-")
        key = (
            (phase, step)
            if phase in ("planner", "orchestrator", "core")
            else (phase, task_id, attempt)
        )
        if key in self._context_seen:
            return
        self._context_seen.add(key)
        total = int(p.get("total_chars") or 0)
        sections = p.get("sections") or {}
        top = self._top_sections(sections)
        line = f"[上下文] {phase} {_fmt_tokens(total)} chars"
        if top:
            line += " · " + top
        self._wline(line, style="dim")

    def _top_sections(self, sections: Any) -> str:
        if not isinstance(sections, dict):
            return ""
        items: list[tuple[str, float]] = []
        for name, val in sections.items():
            if not isinstance(val, dict):
                continue
            pct = float(val.get("pct") or 0)
            items.append((_SECTION_NAMES.get(str(name), str(name)), pct))
        items.sort(key=lambda x: (-x[1], x[0]))
        return " / ".join(f"{name} {pct:.0%}" for name, pct in items[:3])

    def _on_action_started(self, ev: Event) -> None:
        p = ev.payload
        tid = str(p.get("task_id") or self._worker or "?")
        if not self._worker_open or self._worker != tid:
            self._on_dispatch(
                {"task_id": tid, "attempt": p.get("attempt", 0)}
            )
        tool = str(p.get("tool") or "?")
        fp = str(p.get("fp") or "")
        self._actions[(tid, fp)] = {
            "tool": tool,
            "args": p.get("args") or {},
            "ts": ev.ts,
        }
        expect = str(p.get("expect") or "")
        rationale = str(p.get("rationale") or "")
        if expect:
            self._wline(f"[思考] 预期: {expect}", style="magenta")
        if rationale:
            self._wline(f"[思考] 理由: {rationale}", style="magenta")
        args = self._fmt_args(tool, p.get("args") or {})
        self._wline(f"[行动] {tool} {args}".rstrip())

    def _on_action_blocked(self, p: dict) -> None:
        tid = str(p.get("task_id") or self._worker or "?")
        if not self._worker_open or self._worker != tid:
            self._on_dispatch({"task_id": tid, "attempt": p.get("attempt", 0)})
        tool = str(p.get("tool") or "?")
        args = self._fmt_args(tool, p.get("args") or {})
        reason = str(p.get("reason") or "?")
        self._wline(f"[行动] {tool} {args}".rstrip())
        self._wline(f"[拦截] {reason}", style="yellow")

    def _on_action_finished(self, ev: Event) -> None:
        p = ev.payload
        tid = str(p.get("task_id") or self._worker or "?")
        fp = str(p.get("fp") or "")
        action = self._actions.pop((tid, fp), None)
        tool = str(p.get("tool") or (action or {}).get("tool") or "?")
        if action is None:
            args = self._fmt_args(tool, {})
            self._wline(f"[行动] {tool} {args}（缓存命中）".rstrip())
        cached = bool(p.get("cached"))
        exit_code = p.get("exit")
        eid = p.get("evidence")
        dur = ""
        if action is not None:
            ms = int((ev.ts - action["ts"]).total_seconds() * 1000)
            if ms > 0:
                dur = f" · {ms}ms"
        bits = [f"exit={exit_code}"]
        if cached:
            bits.append("缓存命中")
        bits.append(str(eid) if eid else "无证据")
        line = "[观察] " + " · ".join(bits) + dur
        style = "green" if int(exit_code or 0) == 0 else "red"
        self._wline(line, style=style)
        if eid:
            for preview in self._evidence_preview(str(eid))[:2]:
                self._wline("  " + preview, style="dim")

    def _on_clue(self, p: dict) -> None:
        c = p.get("clue") or {}
        try:
            self.clues.append(Clue.model_validate(c))
        except Exception:
            pass
        kind = str(c.get("kind") or "?")
        key = str(c.get("key") or "?")
        value = c.get("value")
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        self._wline(
            f"[线索] {kind} {key} = {_cut(value, 160)}", style="cyan"
        )

    def _on_return(self, p: dict) -> None:
        status = str(p.get("status") or "?")
        if status == "done":
            return
        failure = p.get("failure") or {}
        bits = [f"[返回] {status}"]
        if failure:
            cls = failure.get("cls") or "?"
            detail = failure.get("detail") or ""
            bits.append(f"{cls}: {detail}")
        self._wline(" · ".join(bits), style="red" if status in ("failed", "blocked") else "yellow")

    def _on_state(self, p: dict) -> None:
        tid = str(p.get("task_id") or "?")
        to = str(p.get("to") or "?")
        self.task_states[tid] = to
        if to not in ("done", "failed", "blocked", "superseded"):
            return
        reason = str(p.get("reason") or "")
        attempt = p.get("attempt")
        if to == "done":
            verdict = "success"
            style = "green"
        elif to == "failed":
            verdict = reason or "failure"
            if reason == "virtual_success":
                verdict = "virtual_success（报 done 但判据未满足）"
            style = "red"
        elif to == "blocked":
            verdict = reason or "blocked"
            style = "red"
        else:
            verdict = reason or "superseded"
            style = "dim"
        suffix = f"（attempt {attempt}）" if attempt is not None else ""
        line = f"[判定] {to} · {verdict}{suffix}"
        if self._worker_open and self._worker == tid:
            self._emit_wrapped(line, prefix="  └ ", cont="    ", style=style)
            self._worker_open = False
            self._worker = None
            self._blank()
        else:
            self._emit(f"  [任务] {tid} → {to} · {verdict}{suffix}", style=style)

    # ── 旁路：发现/裁决/教练/记忆/预算 ──────────────────────

    def _on_finding(self, p: dict) -> None:
        f = p.get("finding") or {}
        self.proposed.append(f)
        sev = str(f.get("severity") or "?")
        claim = str(f.get("claim") or "?")
        self._wline(f"[发现] {sev} · {claim}（待裁决）", style="yellow")

    def _on_adjudication(self, p: dict) -> None:
        self.adjudicated.append(p)
        fid = str(p.get("finding_id") or "?")
        verdict = str(p.get("verdict") or "?")
        reason = str(p.get("reason") or "")
        style = "green" if verdict == "confirmed" else "red" if verdict == "false_positive" else "yellow"
        self._wline(f"[JEV] {fid} → {verdict} · {reason}", style=style)

    def _on_coach(self, t: str, p: dict) -> None:
        level = p.get("level") or ""
        issue = p.get("issue") or "?"
        target = p.get("target") or "-"
        if t == "mentor.advisory_agg":
            head = f"[教练] {level} {issue}".replace("  ", " ").strip()
            self._wline(
                f"{head} 再次命中 +{p.get('delta', 1)} · {target}",
                style="yellow",
            )
            return
        bits = [f"[教练] {level} {issue}".replace("  ", " ").strip()]
        metric = p.get("metric")
        if isinstance(metric, dict) and metric:
            bits.append(
                "指标 " + json.dumps(metric, ensure_ascii=False, sort_keys=True)
            )
        would_do = p.get("would_do")
        if would_do:
            bits.append(f"建议 {would_do}")
        bits.append(f"目标 {target}")
        self._wline(" · ".join(bits), style="yellow")

    def _on_intervened(self, p: dict) -> None:
        self._wline(
            f"[教练] {p.get('level', '?')} 介入 · {p.get('issue', '?')} · "
            f"{json.dumps(p.get('detail') or {}, ensure_ascii=False, sort_keys=True)}",
            style="yellow",
        )

    def _on_restart(self, p: dict) -> None:
        self._emit(
            f"  [重试] {p.get('task_id', '?')} · attempt {p.get('attempt', '?')}",
            style="yellow",
        )

    def _on_reflect(self, p: dict) -> None:
        summary = str(p.get("summary") or "")
        self._emit(f"  [记忆] 本轮简报: {_cut(summary, 300)}", style="dim")

    def _on_budget(self, p: dict) -> None:
        self._step += 1
        tokens = int(p.get("tokens") or 0)
        self._spent_tokens += tokens
        self._blank()
        self._emit(
            f"── 第 {self._step} 步结算 · +{_fmt_tokens(tokens)} tokens · "
            f"累计 {_fmt_tokens(self._spent_tokens)} ──",
            style="dim",
        )

    def _on_audit(self, p: dict) -> None:
        broken = p.get("broken") or []
        self._emit(
            f"  [审计] findings={p.get('findings', 0)} · refs={p.get('refs', 0)} · "
            f"可核验={p.get('verified', 0)} · 断链={len(broken)}",
            style="dim",
        )

    # ── 收口 ──────────────────────────────────────────────────

    def _on_finish(self, p: dict) -> None:
        if self._finished:
            return
        self._finished = True
        reason = str(p.get("reason") or "?")
        plateau = p.get("plateau_reason")
        self._blank()
        self._emit("─" * self.width, style="dim")
        head = f"[收口] {reason}" + (f"（{plateau}）" if plateau else "")
        self._emit(head, style="green" if reason == "completed" else "yellow")
        state = self._final_state()
        counts = self._task_counts(state)
        self._kv(
            "任务",
            " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "无",
        )
        self._kv("验收", self._acceptance_line(p, state))
        self._kv("发现", self._finding_line(state))
        tokens = int(getattr(state, "spent_tokens", 0) or self._spent_tokens)
        steps = int(getattr(state, "step", 0) or self._step)
        wall = ""
        if self._first_ts is not None and self._last_ts is not None:
            secs = int((self._last_ts - self._first_ts).total_seconds())
            wall = f" · {_fmt_seconds(secs)}"
        self._kv(
            "成本",
            f"{steps} 步 · {_fmt_tokens(tokens)} tokens{wall}",
        )
        self._kv("报告", str(_report_path(self.root)))
        self._blank()

    def _final_state(self):
        try:
            return StoreView(self.root).restore()
        except Exception as e:
            logger.debug("收口读取投影失败（用事件累计值兜底）: %s", e)
            return None

    def _task_counts(self, state) -> dict[str, int]:
        counts: dict[str, int] = {}
        if state is not None:
            for t in state.tasks.values():
                counts[t.state] = counts.get(t.state, 0) + 1
            return counts
        for s in self.task_states.values():
            counts[s] = counts.get(s, 0) + 1
        return counts

    def _acceptance_line(self, p: dict, state) -> str:
        spec = getattr(state, "spec", None) or self.spec
        if spec is None:
            return "未记录"
        satisfied = set(p.get("satisfied") or [])
        if not satisfied:
            clues = list(getattr(state, "clues", []) or []) if state is not None else self.clues
            satisfied = {
                a.id
                for a in spec.acceptance
                if any(acceptance_hit(c, a) for c in clues)
            }
        total = len(spec.acceptance)
        got = len([a for a in spec.acceptance if a.id in satisfied])
        unmet = [a.id for a in spec.acceptance if a.id not in satisfied]
        line = f"{got}/{total} 达成"
        if unmet:
            line += "（未达成 " + "、".join(unmet) + "）"
        return line

    def _finding_line(self, state) -> str:
        verdicts: list[str] = []
        if state is not None:
            try:
                from pa.adjudicate import finding_id

                for f in state.findings:
                    adj = state.adjudications.get(finding_id(f))
                    verdicts.append(adj.verdict if adj else "undecided")
            except Exception:
                verdicts = []
        if not verdicts:
            verdicts = [str(p.get("verdict") or "undecided") for p in self.adjudicated]
        if not verdicts:
            return "无"
        counts: dict[str, int] = {}
        for v in verdicts:
            counts[v] = counts.get(v, 0) + 1
        return " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))

    # ── 小格式化 ─────────────────────────────────────────────

    def _fmt_args(self, tool: str, args: dict) -> str:
        if not isinstance(args, dict) or not args:
            return ""
        if tool == "http":
            method = str(args.get("method") or "GET").upper()
            return f"{method} {args.get('url') or '?'}"
        if tool in ("probe", "tls"):
            target = args.get("target") or "?"
            port = args.get("port")
            return f"{target}:{port}" if port is not None and ":" not in str(target) else str(target)
        if tool == "dns":
            return str(args.get("target") or "?")
        if tool == "shell":
            return _cut(args.get("cmd") or "", 200)
        if tool == "fs":
            return f"{args.get('op') or 'read'} {args.get('path') or '?'}"
        return _cut(
            json.dumps(args, ensure_ascii=False, sort_keys=True, default=str), 200
        )

    def _fmt_strategy(self, strategy: Any) -> str:
        if isinstance(strategy, dict):
            focus = strategy.get("focus")
            if focus:
                return str(focus)
        return _cut(
            json.dumps(strategy, ensure_ascii=False, sort_keys=True, default=str),
            240,
        )

    def _evidence_preview(self, eid: str) -> list[str]:
        try:
            data = evidence_store.get(self.root, eid)
        except Exception:
            return ["（证据暂不可读）"]
        if not isinstance(data, dict):
            return [f"证据内容不是 JSON 对象: {_cut(data, 160)}"]
        out: list[str] = []
        for key in ("stdout", "stderr"):
            raw = str(data.get(key) or "").strip()
            if not raw:
                continue
            flat = " ".join(raw.split())
            out.append(f"{key}: {_cut(flat, 360)}")
        meta = data.get("meta") or {}
        if isinstance(meta, dict) and meta:
            headers = meta.get("headers") or {}
            bits = []
            if meta.get("content_length") is not None:
                bits.append(f"len={meta['content_length']}")
            if meta.get("location"):
                bits.append(f"Location={meta['location']}")
            if isinstance(headers, dict) and headers:
                bits.append("headers=" + _cut(json.dumps(headers, ensure_ascii=False), 240))
            if bits:
                out.append("meta: " + " · ".join(bits))
        return out or ["（无 stdout/stderr）"]
