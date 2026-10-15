"""TUI 数据层：意图板 / 事件流 / 审批队列的数据装载（无 textual 依赖，可单测）。

与界面解耦：tui_app.py 只做渲染与按键；这里所有函数只读 SQLite/JSONL。
"""

from __future__ import annotations

from pathlib import Path


def resolve_base(job: str = "") -> str:
    """目标任务 base：显式 --job 优先；缺省取最近活跃的任务级意图库；无则空串。"""
    if job:
        return job
    from secpt.cli.cmd_ops import _candidate_task_dbs

    cands = _candidate_task_dbs(limit=1)
    return cands[0][0] if cands else ""


def load_intents(base: str, *, limit: int = 40) -> list[dict]:
    """意图行（按 updated_at 新→旧；base 为空返回空表）。"""
    if not base:
        return []
    try:
        from secpt.core.orchestrator.frontier import Frontier, frontier_db_path

        rows = Frontier(frontier_db_path(base)).list_all(limit=limit)
        rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
        return rows
    except Exception:
        return []


def tail_events(base: str, offset: int) -> tuple[list[str], int]:
    """增量拉取任务事件流（复用 watch.render_lines 的人类可读渲染）。

    返回 (新增渲染行, 新 offset)。base 不存在/无事件文件时返回 ([], offset)。
    """
    if not base:
        return [], offset
    try:
        from secpt.cli.watch import render_lines
        from secpt.core.job.resume import jobs_dir

        path = jobs_dir() / base / "events.jsonl"
        if not path.is_file():
            return [], offset
        lines, new_off = render_lines(path, offset)
        return list(lines), new_off
    except Exception:
        return [], offset


def pending_rows() -> list[dict]:
    """待审批队列（intercept pending）。"""
    try:
        from secpt.governance.intercept import Interceptor

        return Interceptor(seed=False).pending_list()
    except Exception:
        return []


def decide(pending_id: str, approved: bool) -> bool:
    """批准/拒绝一条审批。"""
    try:
        from secpt.governance.intercept import Interceptor

        return Interceptor(seed=False).decide(pending_id, approved)
    except Exception:
        return False


def events_path(base: str) -> Path:
    from secpt.core.job.resume import jobs_dir

    return jobs_dir() / base / "events.jsonl"


__all__ = [
    "decide",
    "events_path",
    "load_intents",
    "pending_rows",
    "resolve_base",
    "tail_events",
]
