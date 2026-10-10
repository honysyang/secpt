"""test_resume：round 落盘→加载→完全一致；sanitize 处理孤立 tool_call。"""

from __future__ import annotations

import json

from secpt.core.job.rounds import (
    load_latest_round,
    load_round,
    sanitize_messages,
    save_round,
)


def _system() -> list[dict]:
    return [{"role": "system", "content": "你是 agent"}]


def test_sanitize_keeps_complete_chain():
    msgs = [
        *_system(),
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs  # 完整链不动


def test_sanitize_drops_orphan_tool_call_tail():
    """尾部 assistant 带 tool_calls 但缺 tool 消息 → 剔除该组。"""
    msgs = [
        *_system(),
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        # 没有对应 tool 消息
    ]
    out = sanitize_messages(msgs)
    assert out == [*_system(), {"role": "user", "content": "do it"}]


def test_sanitize_drops_partial_tools():
    """多个 tool_call 中只回了部分 → 整组剔除，不能留孤立 call。"""
    msgs = [
        *_system(),
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "only a"},
    ]
    out = sanitize_messages(msgs)
    assert out == [*_system(), {"role": "user", "content": "go"}]


def test_sanitize_keeps_multiple_complete_rounds():
    msgs = [
        *_system(),
        {"role": "user", "content": "r1"},
        {
            "role": "assistant",
            "content": "a1",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "out1"},
        {"role": "user", "content": "r2"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs


def test_save_load_round_roundtrip(tmp_path):
    msgs = [
        *_system(),
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "files"},
    ]
    save_round("jb", tmp_path, 0, msgs)
    n, loaded = load_latest_round("jb", tmp_path)
    assert n == 0
    assert loaded == msgs  # 完全一致


def test_save_load_multiple_rounds_latest_wins(tmp_path):
    for r in range(5):
        save_round("jb", tmp_path, r, [*_system(), {"role": "user", "content": f"r{r}"}])
    n, loaded = load_latest_round("jb", tmp_path)
    assert n == 4
    assert loaded == [*_system(), {"role": "user", "content": "r4"}]
    # 指定读旧轮
    r1 = load_round("jb", tmp_path, 1)
    assert r1 == [*_system(), {"role": "user", "content": "r1"}]
    # 不存在的轮
    assert load_round("jb", tmp_path, 99) is None


def test_load_latest_empty(tmp_path):
    n, loaded = load_latest_round("nope", tmp_path)
    assert n == 0
    assert loaded == []


def test_save_sanitizes_on_disk(tmp_path):
    """落盘时若链尾不完整，存档里也应已剔除（resume 不会 400）。"""
    bad_tail = [
        *_system(),
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "x",
            "tool_calls": [
                {
                    "id": "orphan",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                },
            ],
        },
    ]
    save_round("jb", tmp_path, 0, bad_tail)
    _, loaded = load_latest_round("jb", tmp_path)
    assert loaded == [*_system(), {"role": "user", "content": "go"}]
    # 磁盘文件里也不含孤立 call
    raw = json.loads((tmp_path / "jb" / "rounds" / "000.messages.json").read_text())
    assert all(m.get("role") != "assistant" or not m.get("tool_calls") for m in raw["messages"])
