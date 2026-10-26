"""交互式 TUI 跟随（secpt tui）：curses 双栏面板，看「具体交互过程」。

watch 的单行渲染丢掉了 judge 裁决原因（task.state_changed 的 reason）、
expect/rationale、args、clue 全值与证据 stdout/stderr 全文——这些恰是
回答「这一步为什么这样判」的关键字段。tui 用下半屏详情面板全量倒出
payload，证据引用直接读 evidence/ 盘（内容寻址不可变，TUI 只读不写）。

选型说明：curses 是 stdlib——项目三方依赖红线（仅 pydantic/httpx/
pyyaml）排除 rich/textual。无 tty（管道/CI）时回退 follow.watch：
TUI 是给人眼用的，没有交互面就不硬撑。

键位：j/k / ↑↓ 移动 · g/G 首尾 · f 跟随开关 · Tab 详情半屏/全屏 ·
q 退出（mission.finished 后退出码同 run 契约 0/2/3/4）。
"""

from __future__ import annotations

import curses
import json
import sys
import time
from pathlib import Path
from typing import Any

from pa.errors import SecptError
from pa.infra import evidence
from pa.models import Event
from pa.report import follow
from pa.report.follow import _Counters, _PHASE

_STDOUT_CAP = 1500  # stdout 预览上限；全文仍在 evidence/（内容寻址不可变）
_STDERR_CAP = 300
_STR_CAP = 2000  # payload 标量字段展示上限
_JSON_CAP = 4000  # payload 嵌套结构 JSON 展示上限


def _wrap(text: str, width: int) -> list[str]:
    """按宽度折行：优先在空白处断，超长无空白 token 硬切。

    纯函数，详情面板与测试共用；width 至少 1，空串返回单行空串。
    """
    width = max(1, width)
    lines: list[str] = []
    for raw in str(text).splitlines() or [""]:
        if not raw:
            lines.append("")
            continue
        cur = ""
        for tok in raw.split(" "):
            cand = tok if not cur else cur + " " + tok
            if len(cand) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if len(tok) <= width:
                cur = tok
            else:
                for i in range(0, len(tok), width):
                    chunk = tok[i : i + width]
                    if len(chunk) == width:
                        lines.append(chunk)
                    else:
                        cur = chunk
        if cur:
            lines.append(cur)
    return lines


def _evidence_preview(root, eid: str, width: int) -> list[str]:
    """读证据全文渲染 stdout/stderr 预览。

    读不到不炸面板：只在「账本刚记下、盘上亚秒级尚未刷出」或证据真缺
    时出现；SecptError 被显式转为一行说明（可观测，而非静默吞掉）。
    """
    try:
        data = evidence.get(root, eid)
    except SecptError as e:
        return [f"[证据 {eid} 暂不可读: {e}——可能还在写盘，稍后重选]"]
    if not isinstance(data, dict):
        return [f"[证据 {eid} 内容不是 JSON 对象: {_wrap(str(data)[:200], width)}]"]
    lines = [f"── 证据全文 {eid}（内容寻址不可变，TUI 只读）──"]
    for key in ("tool", "exit", "task_id"):
        if key in data:
            lines.append(f"{key}: {data[key]}")
    for key, cap in (("stdout", _STDOUT_CAP), ("stderr", _STDERR_CAP)):
        body = str(data.get(key, "") or "")
        lines.append(f"{key}:")
        lines.extend(_wrap(body, width) if body else ["（空）"])
        if len(body) > cap:
            lines.append(f"…［截断：共 {len(body)} 字符，全文见 evidence/{eid}］")
    return lines


def _field(key: str, value: Any, width: int) -> list[str]:
    """一个 payload 字段 → 展示行：标量一行，str 折行，嵌套结构 pretty JSON。"""
    if isinstance(value, str):
        body = (
            value
            if len(value) <= _STR_CAP
            else value[:_STR_CAP] + f"…（截断，共 {len(value)} 字符）"
        )
        return [f"{key}:"] + (_wrap(body, width) if body else ["（空）"])
    if isinstance(value, (dict, list)):
        j = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if len(j) > _JSON_CAP:
            j = j[:_JSON_CAP] + f"…（截断，共 {len(j)} 字符）"
        return [f"{key}:"] + (_wrap(j, width) or ["（空结构）"])
    return [f"{key}: {value}"]


def detail_lines(ev: Event, root, width: int) -> list[str]:
    """选中事件 → 详情面板行。纯函数（root 只读 evidence 盘，不写），
    与 curses 解耦以便 pytest 覆盖。

    设计取舍：不做 per-type 字段白名单——payload 由 writer 逐型构造、
    随版本演化，白名单迟早静默丢字段（watch 单行渲染的教训）。这里
    按写入顺序全量倒 payload，只在两类上加工：
    ①task.state_changed 顶置 judge 裁决 reason（watch 丢弃的头号信息）；
    ②带证据引用的事件（action.finished / evidence.stored）追加
      stdout/stderr 全文预览。
    """
    width = max(20, int(width))
    ts = ev.ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"#{ev.seq} [{ev.type}] {ev.actor} · {ts} · mission {ev.mission_id[:12]}",
        f"idem: {ev.idem_key or '-'}",
        "",
    ]
    p = ev.payload
    if ev.type == "task.state_changed":
        if p.get("reason"):
            lines.append(f"裁决/原因: {p['reason']}")
            lines.append("")
        lines.append(
            f"状态迁移: {p.get('from', '?')} → {p.get('to', '?')}"
            f"（attempt {p.get('attempt', '?')}）"
        )
        lines.append("")
    for key, value in p.items():
        lines.extend(_field(key, value, width))
    eid = p.get("evidence") or (p.get("id") if ev.type == "evidence.stored" else None)
    if isinstance(eid, str) and eid.startswith("ev-"):
        lines.append("")
        lines.extend(_evidence_preview(root, eid, width))
    if ev.type == "mission.finished":
        lines.append("")
        rp = Path(root)
        # 报告统一落家族根（save_report 家族级聚合），与 follow.watch 同口径
        report_dir = rp.parent.parent if rp.parent.name == "runs" else rp
        lines.append(f"报告: {(report_dir / 'report.md').resolve()}")
    return lines


class _Ledger:
    """增量 tail events.jsonl：与 follow.watch 相同的字节偏移协议
    （半行 buf 留待下次），渲染行在 poll 时算好缓存，curses 帧只取用。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.buf = ""
        self.events: list[Event] = []
        self.rows: list[str] = []
        self.durs: dict[int, int] = {}
        self.counters = _Counters()
        self.dispatched_at: dict[str, Any] = {}
        self.step = 0
        self.finished: dict[str, Any] | None = None

    def poll(self) -> int:
        """读入新增整行事件，返回本次新增数（目录未建/无新增均 0）。"""
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as f:
            f.seek(self.offset)
            data = f.read()
            self.offset = f.tell()
        self.buf += data
        added = 0
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            ev = Event.model_validate_json(line)
            dur_ms: int | None = None
            if ev.type == "task.dispatched":
                self.dispatched_at[str(ev.payload.get("task_id", ""))] = ev.ts
            elif ev.type == "worker.returned":
                started = self.dispatched_at.pop(
                    str(ev.payload.get("task_id", "")), None
                )
                if started is not None:
                    dur_ms = int((ev.ts - started).total_seconds() * 1000)
                    self.durs[ev.seq] = dur_ms
            if ev.type == "budget.charged":
                self.step += 1
            self.events.append(ev)
            self.rows.append(follow.render_line(ev, step=self.step, dur_ms=dur_ms))
            self.counters.feed(ev)
            if ev.type == "mission.finished":
                self.finished = ev.payload
            added += 1
        return added

    @property
    def exit_code(self) -> int | None:
        """mission.finished 的退出码（未收口 None；q 由用户决定退出）。"""
        if self.finished is None:
            return None
        return follow._EXIT.get(str(self.finished.get("reason", "error")), 1)


_FG_PHASE: tuple[tuple[str, int], ...] = (
    ("boot", curses.COLOR_CYAN),
    ("plan", curses.COLOR_YELLOW),
    ("decide", curses.COLOR_CYAN),
    ("dispatch", curses.COLOR_MAGENTA),
    ("task", curses.COLOR_YELLOW),
    ("action", curses.COLOR_GREEN),
    ("evidence", curses.COLOR_GREEN),
    ("clue", curses.COLOR_CYAN),
    ("worker", curses.COLOR_WHITE),
    ("finding", curses.COLOR_YELLOW),
    ("settle", curses.COLOR_BLUE),
    ("context", curses.COLOR_BLUE),
    ("mentor", curses.COLOR_MAGENTA),
    ("finish", curses.COLOR_GREEN),
    ("other", curses.COLOR_WHITE),
)


def _init_pairs() -> dict[str, int]:
    """按 phase 建 curses 颜色对；pair 编号从 1 起（0 是默认白）。"""
    pair_of: dict[str, int] = {}
    for i, (phase, fg) in enumerate(_FG_PHASE):
        curses.init_pair(i + 1, fg, -1)
        pair_of[phase] = i + 1
    return pair_of


def _fit(s: str, w: int) -> str:
    """截到不超过 curses 窗口宽-1（写满最后一格在部分终端会折行错位）。"""
    return s[: max(0, w - 1)]


def _phase_pair(ev: Event, pair_of: dict[str, int]) -> int:
    phase = _PHASE.get(ev.type, ev.type.split(".")[0])
    return pair_of.get(phase, 0)


def _draw(
    stdscr,
    ledger: _Ledger,
    sel: int,
    follow_mode: bool,
    detail_max: bool,
    pair_of: dict[str, int],
    detail_cache: dict[int, list[str]],
) -> None:
    """一帧渲染：header / 事件列表 / 详情面板 / footer。只读 ledger。"""
    from pa.policy.coach import mode as mentor_mode

    h, w = stdscr.getmaxyx()
    stdscr.erase()
    # header：账本口径计数（与 watch 底部状态行同源）
    stdscr.attron(curses.A_REVERSE)
    stdscr.addnstr(0, 0, _fit(ledger.counters.status(mentor_mode()) + "  ", w), w - 1)
    stdscr.attroff(curses.A_REVERSE)

    body = max(1, h - 3)  # header(1) + footer(2) 之外的行数
    if detail_max:
        list_h, detail_h = 0, body  # Tab 全屏：列表隐去，详情独占
    else:
        list_h = max(1, body // 2)
        detail_h = max(1, body - list_h)

    if ledger.events and list_h:
        start = max(0, min(sel, len(ledger.rows) - 1) - list_h + 1)
        end = min(len(ledger.rows), start + list_h)
        for row_y, idx in enumerate(range(start, end), start=1):
            attr = curses.color_pair(_phase_pair(ledger.events[idx], pair_of))
            if idx == sel:
                attr |= curses.A_REVERSE | curses.A_BOLD
            mark = "▶ " if idx == sel else "  "
            stdscr.addnstr(row_y, 0, _fit(mark + ledger.rows[idx], w), w - 1, attr)

    detail_y = 1 + list_h
    if ledger.events and detail_h:
        ev = ledger.events[min(sel, len(ledger.events) - 1)]
        lines = detail_cache.get(ev.seq)
        if lines is None:
            lines = detail_lines(ev, ledger.path.parent, w)
            detail_cache[ev.seq] = lines
        page = lines[-detail_h:]
        first = len(lines) - len(page) + 1
        stdscr.addnstr(
            detail_y,
            0,
            _fit(f"── 详情 {first}-{len(lines)}/{len(lines)}", w),
            w - 1,
            curses.A_DIM,
        )
        for row_y, ln in enumerate(page, start=detail_y + 1):
            stdscr.addnstr(row_y, 0, _fit(ln, w), w - 1)

    # footer：finish 信息占 h-2，键位提示恒占 h-1
    if ledger.finished is not None:
        f = ledger.finished
        lp = ledger.path.parent
        # 报告统一落家族根（save_report 家族级聚合），与 follow.watch 同口径
        report_dir = lp.parent.parent if lp.name == "runs" else lp
        msg = (
            f"终止：{f.get('reason', '?')} —— {f.get('summary', '')}  "
            f"报告：{(report_dir / 'report.md').resolve()}"
        )
        stdscr.addnstr(h - 2, 0, _fit(msg, w), w - 1, curses.A_BOLD)
    else:
        stdscr.addnstr(
            h - 2, 0, _fit(f"已渲染 {len(ledger.rows)} 条", w), w - 1, curses.A_DIM
        )
    mode_tag = "跟随" if follow_mode else "回看"
    keys = f"{mode_tag} │ j/k ↑↓ 移动  g/G 首尾  f 跟随  Tab 详情放大  q 退出"
    stdscr.attron(curses.A_REVERSE)
    stdscr.addnstr(h - 1, 0, _fit(keys + " " * 40, w), w - 1)
    stdscr.attroff(curses.A_REVERSE)
    stdscr.refresh()


def _run(stdscr, root: Path, poll: float) -> int:
    """curses 主循环。节拍 = stdscr.timeout(poll*1000)：getch 超时返回
    -1 正好当了轮询时钟，不需要额外线程/信号。"""
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    pair_of = _init_pairs()
    stdscr.timeout(int(poll * 1000))
    ledger = _Ledger(root / "events.jsonl")
    sel = 0
    follow_mode = True
    detail_max = False
    detail_cache: dict[int, list[str]] = {}
    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            code = ledger.exit_code
            return 0 if code is None else code
        if key in (curses.KEY_UP, ord("k")):
            follow_mode = False
            sel = max(0, sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            follow_mode = False
            sel = sel + 1
        elif key == ord("g"):
            follow_mode = False
            sel = 0
        elif key == ord("G"):
            sel = 10**9
        elif key == ord("f"):
            follow_mode = not follow_mode
        elif key == 9:  # Tab：详情半屏 ⇄ 全屏（列表隐去）
            detail_max = not detail_max
        # 其余按键（含 -1 超时与 KEY_RESIZE）一并忽略，进入下一帧重画
        ledger.poll()
        last = max(0, len(ledger.events) - 1)
        if follow_mode:
            sel = last
        sel = min(sel, last)
        _draw(stdscr, ledger, sel, follow_mode, detail_max, pair_of, detail_cache)


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def watch(root, *, poll: float = 0.2, wait: float = 30.0) -> int:
    """TUI 跟随 mission 事件账本（只读：不产生事件、不回写任何文件）。

    缺 events.jsonl 时等待出现（run --follow 下 bootstrap 稍后建目录）；
    非 tty 回退 follow.watch 单行渲染。Ctrl-C 由调用方（cli._tui）捕获
    返回 130。"""
    root = Path(root)
    path = root / "events.jsonl"
    deadline = time.monotonic() + wait
    while not path.exists():
        if time.monotonic() > deadline:
            raise SecptError(f"等待事件账本超时: {path}")
        time.sleep(poll)
    if not _is_tty():
        return follow.watch(root)
    return curses.wrapper(_run, root, poll)
