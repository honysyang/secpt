"""非阻塞 compaction 契约测试（任务一验收）。

主循环只做：删组 + 插占位 + 落请求文件（微秒级）+ compaction_requested 事件；
后台 worker 拉请求生成摘要 → 写 <k>.summary.md → compaction_completed 事件；
主循环下一轮把占位回填为真摘要。摘要文件是完成真相源。
"""

from __future__ import annotations

import asyncio

from secpt.core.context.compaction import (
    PLACEHOLDER_MARK,
    complete_request_summary,
    load_summary,
    placeholder_content,
    plan_compaction,
    read_pending_requests,
    should_compact,
    swap_summary_placeholders,
    write_compaction_request,
)


def _big(role: str = "user", n: int = 30_000) -> dict:
    return {"role": role, "content": "x" * n}


class _StubSummarizer:
    """桩摘要器：返回固定格式摘要（验证 complete 链路，不调 LLM）。"""

    def __init__(self):
        self.calls = 0

    async def __call__(self, objective, msgs):
        self.calls += 1
        return f"摘要({len(msgs)}条)"


class _FakeSink:
    def __init__(self):
        self.calls: list[tuple] = []

    def on_compaction_requested(self, k, removed, window):
        self.calls.append(("requested", k, removed, window))

    def on_compaction_completed(self, k, summary, removed, window):
        self.calls.append(("completed", k, summary, removed, window))

    def on_compaction(self, k, summary, removed, window):
        self.calls.append(("legacy", k, summary, removed, window))


def _chain(groups: int = 6) -> list[dict]:
    """构造完整组链：system + (assistant(tool_calls)+tool)×N——只有完整组才可删。"""
    msgs = [{"role": "system", "content": "s"}]
    for i in range(groups):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 30_000})
    return msgs


def test_plan_compaction_deletes_groups_protects_tail():
    """超阈值 → 返回删组计划；system 首条与尾部保护段保留。"""
    msgs = _chain()
    assert should_compact(msgs, 10_000)
    plan = plan_compaction(msgs, 10_000)
    assert plan is not None
    remaining, removed, first_idx = plan
    assert len(removed) >= 2 and len(remaining) < len(msgs)
    assert remaining[0]["role"] == "system"
    # 组完整性：删的都是完整组（assistant+tool 成对）
    removed_ids = {m.get("tool_call_id") for m in removed if m.get("role") == "tool"}
    assert len(removed_ids) * 2 == len(removed)
    assert first_idx >= 1


def test_plan_no_compaction_when_small():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert plan_compaction(msgs, 60_000) is None


def test_request_queue_and_background_completion(tmp_path):
    """请求入队 → 后台完成：写摘要文件 + 摘除请求 + 发 completed/legacy 事件。"""
    req_path = write_compaction_request(
        "jx",
        tmp_path,
        k=3,
        window=60_000,
        objective="目标",
        removed_msgs=[{"role": "user", "content": "a" * 100}],
    )
    assert req_path.name == "003.json"
    reqs = read_pending_requests(tmp_path, "jx")
    assert len(reqs) == 1 and reqs[0][1]["k"] == 3

    sink = _FakeSink()
    stub = _StubSummarizer()
    asyncio.new_event_loop().run_until_complete(
        complete_request_summary(req_path, reqs[0][1], stub, sink)
    )
    assert load_summary(tmp_path, "jx", 3) == "摘要(1条)"
    assert not req_path.exists()  # 请求已摘除
    assert stub.calls == 1
    kinds = [c[0] for c in sink.calls]
    assert kinds == ["completed", "legacy"]  # 新事件 + watch 兼容
    assert sink.calls[0][2] == "摘要(1条)"


def test_placeholder_swap_backfills(tmp_path):
    """占位回填：摘要未生成 → 不动；生成后 → 占位替换为真摘要。"""
    req_path = write_compaction_request(
        "jy", tmp_path, k=1, window=60_000, objective="o", removed_msgs=[]
    )
    _, req = read_pending_requests(tmp_path, "jy")[0]
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": placeholder_content(1)},
    ]
    pending = {1}

    # 未生成 → 不回填
    assert swap_summary_placeholders(msgs, tmp_path, "jy", pending) == 0
    assert pending == {1}

    # 后台完成 → 回填
    asyncio.new_event_loop().run_until_complete(
        complete_request_summary(req_path, req, _StubSummarizer(), _FakeSink())
    )
    assert swap_summary_placeholders(msgs, tmp_path, "jy", pending) == 1
    assert pending == set()
    assert PLACEHOLDER_MARK not in msgs[1]["content"]
    assert "摘要(0条)" in msgs[1]["content"]
    assert msgs[1]["content"].startswith("【历史摘要 #1】")


def test_complete_summary_failure_writes_fallback(tmp_path):
    """摘要器连续失败 → 写失败占位摘要并照常完成（防 pending 堆积空转）。"""

    class _Boom:
        async def __call__(self, objective, msgs):
            raise RuntimeError("llm down")

    req_path = write_compaction_request(
        "jz", tmp_path, k=2, window=60_000, objective="o", removed_msgs=[]
    )
    _, req = read_pending_requests(tmp_path, "jz")[0]
    asyncio.new_event_loop().run_until_complete(
        complete_request_summary(req_path, req, _Boom(), _FakeSink())
    )
    s = load_summary(tmp_path, "jz", 2)
    assert s is not None and "摘要生成失败" in s
    assert not req_path.exists()
