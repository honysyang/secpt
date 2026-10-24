"""test_events：事件 seq 连续；tool_result 带 status/bytes；SQLite 挂了 jsonl 仍写入。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from secpt.core.job.event import EVENT_TYPES, EventSink
from secpt.core.job.store import JobStore


def _events_from_file(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_seq_contiguous(tmp_path):
    sink = EventSink("j1", tmp_path, store=None)
    a = sink.write("assistant", {"content": "hi"})
    b = sink.write("tool_call", {"tool": "bash", "args": "{}"})
    c = sink.write("tool_result", {"status": "ok", "bytes": 3})
    assert (a, b, c) == (1, 2, 3)
    events = _events_from_file(tmp_path / "j1" / "events.jsonl")
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_unknown_event_type_rejected(tmp_path):
    sink = EventSink("j1", tmp_path, store=None)
    import pytest

    with pytest.raises(ValueError):
        sink.write("wat", {})


def test_tool_result_status_classification(tmp_path):
    sink = EventSink("j1", tmp_path, store=None)
    sink.on_result("c1", "")
    sink.on_result("c2", "Error: timeout 60s")
    sink.on_result("c3", "real output 123")
    events = _events_from_file(tmp_path / "j1" / "events.jsonl")
    results = [e["payload"] for e in events if e["type"] == "tool_result"]
    assert [r["status"] for r in results] == ["empty", "error", "ok"]
    assert [r["bytes"] for r in results] == [0, len("Error: timeout 60s"), len("real output 123")]


def test_jsonl_primary_sqlite_secondary(tmp_path):
    store = JobStore(tmp_path / "t.sqlite")
    sink = EventSink("j1", tmp_path, store=store)
    sink.on_assistant({"content": "think", "tool_calls": []})
    sink.on_tool("c8", "bash", {"command": "ls"})
    sink.on_result("c9", "out")
    sink.on_round_end(1, [{"role": "system", "content": "s"}])

    # jsonl 侧 4 条
    assert len(_events_from_file(tmp_path / "j1" / "events.jsonl")) == 4
    # sqlite 侧同步 4 条
    assert len(store.get_events("j1")) == 4


def test_sqlite_failure_does_not_block_jsonl(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "t.sqlite")
    sink = EventSink("j1", tmp_path, store=store)

    def boom(job_id, etype, payload, round_=0):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "add_event", boom)
    sink.write("tool_result", {"status": "ok", "bytes": 1})
    sink.write("assistant", {"content": "x"})
    # SQLite 全挂，jsonl 不受影响
    assert len(_events_from_file(tmp_path / "j1" / "events.jsonl")) == 2


def test_hooks_all_wired(tmp_path):
    sink = EventSink("j1", tmp_path, store=None)
    for hook in ("on_step", "on_assistant", "on_tool", "on_result", "on_round_end"):
        assert callable(getattr(sink, hook)), f"missing hook {hook}"
    # 事件类型全集（S3：compaction；S4：routed；Phase A：verdict 评判层写入；
    # 非阻塞 compaction：tokens/compaction_requested/compaction_completed）
    assert {
        "assistant",
        "tool_call",
        "tool_result",
        "finding",
        "round_end",
        "budget_warning",
        "rollover",
        "status_change",
        "error",
        "compaction",
        "routed",
        "step",
        "user_msg",
        "verdict",
        "tokens",
        "compaction_requested",
        "compaction_completed",
        "loop_phase",  # 三档止损状态机档位迁移（设计 §2）
        "plan_event",  # v4 编排：Commander/规划轮结果（frontier 父流直写）
        "intent_event",  # v4 编排：意图生命周期（领取/结算轨迹）
        "approval_request",  # 治理拦截 ask：待审批命令（Web 审批卡数据源）
        "approval_decision",  # 治理拦截：审批结果回执
    } == EVENT_TYPES


def test_tokens_and_compaction_events(tmp_path):
    """on_tokens 落 tokens 事件（checkpoint 恢复预算的数据源）；
    on_compaction_requested/_completed 落对应事件（非阻塞 compaction 验收）。"""
    sink = EventSink("j-events2", tmp_path, store=None)
    sink.on_tokens(1000, 200)
    sink.on_compaction_requested(0, 42, 60000)
    sink.on_compaction_completed(0, "六段摘要", 42, 60000)
    lines = [
        json.loads(line)
        for line in (tmp_path / "j-events2" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    types = [ev["type"] for ev in lines]
    assert types == ["tokens", "compaction_requested", "compaction_completed"]
    payload = lines[0]["payload"]
    assert payload["prompt"] == 1000 and payload["completion"] == 200
    assert payload["cost_cny"] > 0
