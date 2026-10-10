"""secpt watch：实时交互日志视图。

把 append-only 事件流渲染成人读的逐步执行视图：

    ── step 12 (45s) ──────────────────────────
    💭 思考：登录接口没有验证码，口令策略是 6 位……
    🔧 bash: curl -s -X POST http://target/login -d 'user=admin'
    📥 200 OK | {"token": "..."}                    ← 工具响应（截断）
    ★ finding: credential password=admin123

用法：
    secpt watch <job_id>          # 实时跟随（Ctrl-C 退出）
    secpt watch <job_id> --tail 50   # 只看最近 50 条事件
    secpt watch <job_id> --raw       # 原始 JSONL（调试）
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

_OUT_LEN = 360  # 工具响应预览长度
_THINK_LEN = 280  # 思考预览长度


def _job_events_path(job_id: str) -> Path:
    from secpt.core.job.resume import jobs_dir

    return jobs_dir() / job_id / "events.jsonl"


def render_event(ev: dict) -> str | None:
    """单事件 → 人读一行/多行。None = 不显示。"""
    etype = ev.get("type")
    p = ev.get("payload") or {}
    if etype == "step":
        return f"\n── step {p.get('n')} ({p.get('elapsed_s', 0)}s) " + "─" * 40
    if etype == "assistant":
        lines = []
        reasoning = (p.get("reasoning") or "").strip()
        if reasoning:
            lines.append(f"💭 思考: {reasoning[:_THINK_LEN]}")
        content = (p.get("content") or "").strip()
        if content and not content.startswith("ANSWER"):
            lines.append(f"💬 说: {content[:_THINK_LEN]}")
        if (content or "").startswith("ANSWER"):
            lines.append(f"🏁 {content[:200]}")
        # 注意：即将发生的工具调用由紧随的 tool_call 事件渲染（不重复预告）
        return "\n".join(lines) if lines else None
    if etype == "tool_call":
        return f"🔧 {p.get('tool')}: {p.get('args', '')[:220]}"
    if etype == "tool_result":
        status = p.get("status", "?")
        icon = {"ok": "📥", "error": "⚠️ ", "empty": "📥"}.get(status, "📥")
        return f"{icon} [{status}] {p.get('preview', '')[:_OUT_LEN]}"
    if etype == "finding":
        return f"★ 发现 [{p.get('kind')}]: {p.get('value', '')[:140]}"
    if etype == "compaction":
        return f"🗜 压缩 #{p.get('k', '?')}: 删 {p.get('removed_messages')} 条"
    if etype == "rollover":
        return f"🔁 会话轮转 → round {p.get('round')}（新上下文链）"
    if etype == "budget_warning":
        return f"💰 预算警告: {p.get('message', '')[:120]}"
    if etype == "status_change":
        return f"📌 状态: {p.get('status')}"
    if etype == "error":
        return f"❌ 错误: {p.get('message', '')[:200]}"
    if etype == "verdict":
        ok = "✅" if p.get("complete") else "❌"
        return f"{ok} 评判: {p.get('method')} confidence={p.get('confidence')}"
    return None  # round_end 等结构性事件不刷屏


def render_lines(path: Path, start_seq: int = 0) -> tuple[list[str], int]:
    """读取 seq > start_seq 的事件并渲染。返回 (行列表, 最新 seq)。"""
    out: list[str] = []
    last = start_seq
    if not path.is_file():
        return out, last
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            last = max(last, int(ev.get("seq", 0)))
            if int(ev.get("seq", 0)) <= start_seq:
                continue
            rendered = render_event(ev)
            if rendered:
                out.append(rendered)
    return out, last


def watch(job_id: str, *, tail: int = 0, raw: bool = False, poll_s: float = 1.0) -> None:
    path = _job_events_path(job_id)
    if not path.is_file():
        print(f"Error: job {job_id} 无事件流: {path}")
        raise SystemExit(2)

    if raw:
        # 原始 JSONL 模式
        import sys

        start = 0
        if tail:
            lines = path.read_text(encoding="utf-8").splitlines()[-tail:]
            for line in lines:
                print(line)
            start = (len(lines) and json.loads(lines[-1]).get("seq", 0)) or 0
        try:
            with open(path, encoding="utf-8") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        print(line.rstrip())
                        sys.stdout.flush()
                    else:
                        time.sleep(poll_s)
        except KeyboardInterrupt:
            return

    start = 0
    if tail:
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line))
        events = events[-tail:]
        for ev in events:
            r = render_event(ev)
            if r:
                print(r)
        start = events[-1].get("seq", 0) if events else 0

    print(f"👀 watching {job_id}（Ctrl-C 退出）")
    try:
        while True:
            lines, start = render_lines(path, start)
            for line in lines:
                print(line)
            time.sleep(poll_s)
    except KeyboardInterrupt:
        print("\n（已退出 watch，任务仍在后台运行）")
