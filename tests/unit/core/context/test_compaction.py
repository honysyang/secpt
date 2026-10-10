"""test_compaction：压缩后 messages 无孤立 tool_call；整组删除；保护尾部。

验收判据：
- 压缩后 messages 里没有孤立 tool_call（模拟请求不报错）
- 整组删除：assistant[含 tool_calls] + 它的全部 tool messages 一起删
- 首条 system 与最新 12 条保留；原位插一条【历史摘要】user 消息
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from secpt.core.context.compaction import compact_messages, should_compact
from secpt.core.context.summarize import StubSummarizer
from secpt.runtime.llm.client import sanitize_messages


def _group(i: int, n_calls: int = 1) -> list[dict]:
    """构造第 i 个完整组：assistant(带 tool_calls) + 全部 tool 结果。"""
    calls = [
        {
            "id": f"c{i}-{j}",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": f"cmd {i}-{j}"})},
        }
        for j in range(n_calls)
    ]
    msgs = [{"role": "assistant", "content": f"分析第 {i} 组", "tool_calls": calls}]
    for tc in calls:
        msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": "out " + "x" * 300})
    return msgs


def _assert_no_orphan(messages: list[dict]) -> None:
    """链里不允许孤立 tool_call / 孤立 tool 消息。"""
    declared: set[str] = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            declared.add(tc["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in declared, "孤立 tool 消息（缺 assistant）"
    # 每个带 tool_calls 的 assistant 都必须完整（否则模拟请求会 400）
    for i, m in enumerate(messages):
        if not (m.get("tool_calls") or []):
            continue
        needed = {tc["id"] for tc in m["tool_calls"]}
        have = 0
        for x in messages[i + 1 :]:
            if x.get("role") == "tool" and x.get("tool_call_id") in needed:
                have += 1
        assert have >= len(needed), f"assistant@{i} 的 tool 结果缺失（孤立 tool_call）"


def _chain() -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "你是渗透测试员。"}]
    for i in range(12):
        msgs += _group(i)
    return msgs


def test_compaction_removes_groups_and_keeps_chain_valid(tmp_path: Path) -> None:
    chain = _chain()
    window = 2000
    assert should_compact(chain, window)
    before = len(chain)
    new, removed, _summary = asyncio.run(
        compact_messages(chain, "任务目标：找到 flag", StubSummarizer(), window)
    )
    assert removed > 0
    assert len(new) < before
    # 无孤立 tool_call / tool 消息
    _assert_no_orphan(new)
    # 模拟请求不报错（剔除 reasoning_content 后结构完整）
    req = sanitize_messages(new)
    assert isinstance(req, list)
    # 首条仍是 system
    assert new[0]["role"] == "system"
    # 原位插入了【历史摘要】user 消息
    assert any(
        m.get("role") == "user" and str(m.get("content", "")).startswith("【历史摘要 #")
        for m in new
    )
    # 摘要含"已排除"段落（StubSummarizer 保证）
    summary_msg = next(
        m
        for m in new
        if m.get("role") == "user" and str(m.get("content", "")).startswith("【历史摘要")
    )
    assert "已排除的尝试" in summary_msg["content"]


def test_compaction_protects_latest_messages(tmp_path: Path) -> None:
    chain = _chain()
    chain[-2]["content"] = "TAIL_MARKER_ASSISTANT"  # 最新一组标记
    window = 2000
    new, _, _ = asyncio.run(compact_messages(chain, "obj", StubSummarizer(), window))
    joined = json.dumps(new, ensure_ascii=False)
    # 最新消息（尾组）未被删除
    assert "TAIL_MARKER_ASSISTANT" in joined


def test_compaction_group_atomic_multi_toolcall(tmp_path: Path) -> None:
    """一个 assistant 带 2 个 tool_call：整组删除，绝不留下单个 tool 结果。"""
    chain = [{"role": "system", "content": "sys"}]
    chain += _group(0, n_calls=2)  # A 有两个 tool 结果
    chain += _group(1, n_calls=1)  # B 有一个
    window = 400
    # 保护尾部按 0：链很短，本测试专注"整组原子删除"语义
    new, removed, _ = asyncio.run(
        compact_messages(chain, "obj", StubSummarizer(), window, protected_tail=0)
    )
    assert removed > 0
    _assert_no_orphan(new)
    # 不能出现孤立的 c0-0/c0-1 的 tool 消息
    for m in new:
        if m.get("role") == "tool":
            assert m["tool_call_id"] != "c0-0"
            assert m["tool_call_id"] != "c0-1"


def test_est_tokens_floor() -> None:
    from secpt.core.context.estimator import est_tokens

    assert est_tokens("") == 0
    assert est_tokens("x") >= 1
