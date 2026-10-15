"""secpt tui：多任务运维面板（textual，可选依赖 pip install 'secpt[tui]'）。

7×24 运维的观察面在一块屏上：意图板（左）+ 事件流（右）+ 审批队列（底）。
自动 1s 刷新；按键：q 退出 / r 立即刷新 / a 批准首个待审批 / d 拒绝首个待审批。
只做观测与审批——直投/编辑意图仍走 `secpt intent` 命令（TUI 不抢 CLI 职责）。
"""

from __future__ import annotations

from typing import ClassVar

import typer


def make_app(job: str = ""):
    """构建 TUI App 实例（测试/嵌入式用；textual 缺失时抛 ImportError）。"""
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import DataTable, Footer, Header, RichLog, Static

    from secpt.cli import tui_data

    class SecptTUI(App):
        """意图板 + 事件流 + 审批队列，自动刷新。"""

        TITLE = "secpt 运维面板"
        BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
            ("q", "quit", "退出"),
            ("r", "refresh_now", "刷新"),
            ("a", "approve_first", "批准首条"),
            ("d", "deny_first", "拒绝首条"),
        ]
        CSS = """
        #jobbar { height: 1; padding: 0 1; background: $boost; }
        #panes { height: 1fr; }
        #intents { width: 42%; }
        #events { width: 58%; }
        #pending { height: 8; }
        """

        def __init__(self, base: str = "") -> None:
            super().__init__()
            self.base = base
            self._ev_off = 0
            self._last_intents_sig = ""

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("", id="jobbar")
            with Horizontal(id="panes"):
                yield DataTable(id="intents")
                yield RichLog(id="events", markup=False, wrap=True)
            yield DataTable(id="pending")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#intents", DataTable).add_columns("id", "源", "状态", "p", "标题")
            self.query_one("#pending", DataTable).add_columns("id", "规则", "命令预览", "状态")
            self.refresh_panes()
            self.set_interval(1.0, self.refresh_panes)

        def _base(self) -> str:
            if not self.base:
                self.base = tui_data.resolve_base("")
            return self.base

        def refresh_panes(self) -> None:
            base = self._base()
            self.query_one("#jobbar", Static).update(
                f"任务: {base or '（无任务级意图库——先 secpt hunt/solve）'}"
                f"   刷新: 1s 自动   q 退出 / a 批准 / d 拒绝"
            )
            # 意图板
            rows = tui_data.load_intents(base)
            sig = "|".join(f"{r.get('id')}:{r.get('status')}:{r.get('priority')}" for r in rows)
            if sig != self._last_intents_sig:
                self._last_intents_sig = sig
                table = self.query_one("#intents", DataTable)
                table.clear()
                for r in rows:
                    tag = "★人" if r.get("source") == "direct" else "#AI"
                    table.add_row(
                        str(r.get("id", "")),
                        tag,
                        str(r.get("status", "")),
                        str(r.get("priority", "")),
                        str(r.get("title", ""))[:40],
                    )
            # 事件流（增量）
            lines, self._ev_off = tui_data.tail_events(base, self._ev_off)
            if lines:
                log = self.query_one("#events", RichLog)
                for ln in lines[-200:]:
                    log.write(str(ln))
            # 审批队列
            pending = tui_data.pending_rows()
            ptable = self.query_one("#pending", DataTable)
            ptable.clear()
            for p in pending:
                ptable.add_row(
                    str(p.get("id", "")),
                    str(p.get("rule_name", "")),
                    str(p.get("preview", ""))[:50],
                    str(p.get("status", "")),
                )

        # ---------------- 动作 ----------------

        def action_refresh_now(self) -> None:
            self.refresh_panes()

        def _decide_first(self, approved: bool) -> None:
            pending = tui_data.pending_rows()
            if not pending:
                self.notify("当前无待审批")
                return
            pid = str(pending[0]["id"])
            ok = tui_data.decide(pid, approved)
            self.notify(f"{'已批准' if approved else '已拒绝'} {pid}" if ok else f"操作失败 {pid}")
            self.refresh_panes()

        def action_approve_first(self) -> None:
            self._decide_first(True)

        def action_deny_first(self) -> None:
            self._decide_first(False)

    return SecptTUI(job)


def run_tui(job: str = "") -> None:
    """启动 TUI（textual 缺失 → 明确安装指引）。"""
    try:
        app = make_app(job)
    except ImportError:
        typer.echo(
            "Error: 需要 textual（pip install 'secpt[tui]'）。"
            "轻量替代：secpt watch <job_id>（事件流）/ secpt intent list --job <base>",
            err=True,
        )
        raise typer.Exit(2) from None
    app.run()
