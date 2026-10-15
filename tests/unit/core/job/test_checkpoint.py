"""任务二验收：事件驱动恢复检查点（round/预算/detector/steps/摘要编号）。"""

from __future__ import annotations

import asyncio
import json

from secpt.core.context.budget import BudgetTracker, tokens_cost
from secpt.core.job.event import EventSink
from secpt.core.job.resume import load_last_checkpoint
from secpt.core.job.rounds import save_round


def _make_archive(tmp_path, job_id="jck"):
    """构造一份完整存档：round 快照 + 事件流（tokens/nudge/compaction/step）。

    存档基目录 = tmp_path/"jobs"（与 engine autoresume 走的 jobs_dir() 对齐：
    SECPT_VAR=tmp_path 时 jobs_dir() == tmp_path/jobs）。
    """
    base = tmp_path / "jobs"
    sink = EventSink(job_id, base, store=None)
    save_round(
        job_id,
        base,
        3,
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ],
    )
    sink.on_tokens(10_000, 2_000)
    sink.on_tokens(20_000, 4_000)
    sink.write("user_msg", {"kind": "nudge", "text": "x"})
    sink.write("user_msg", {"kind": "steer", "text": "y"})  # 非 nudge 不计
    sink.on_compaction_requested(0, 5, 60_000)
    sink.write("step", {"n": 7})
    sink.write("step", {"n": 9})


def test_checkpoint_aggregates_events(tmp_path):
    _make_archive(tmp_path)
    cp = load_last_checkpoint("jck", tmp_path / "jobs")
    assert cp is not None
    assert cp["round"] == 3
    assert cp["messages"][0]["role"] == "system"
    assert cp["prompt_tokens"] == 30_000
    assert cp["completion_tokens"] == 6_000
    expected_cost = round(tokens_cost(30_000, 6_000), 6)
    assert abs(cp["cost_cny"] - expected_cost) < 1e-6
    assert cp["nudges"] == 1  # steer 不计入
    assert cp["summary_k"] == 1  # k=0 事件 → 已发生 1 次压缩
    assert cp["steps"] == 9


def test_checkpoint_absent_when_fresh(tmp_path):
    assert load_last_checkpoint("nope", tmp_path) is None


def test_checkpoint_without_rounds_returns_none(tmp_path):
    """只有事件流、无 round 快照 → 不可恢复（返回 None，走全新链）。"""
    sink = EventSink("j-ev-only", tmp_path, store=None)
    sink.on_tokens(100, 10)
    assert load_last_checkpoint("j-ev-only", tmp_path) is None


def test_budget_seed_semantics():
    """恢复语义：tokens 记入累计后，add() 按总额重算 cost（与种子一致）。"""
    b = BudgetTracker(10.0)
    # 模拟 engine 恢复种子
    b.prompt_tokens += 30_000
    b.completion_tokens += 6_000
    b.cost_cny = tokens_cost(30_000, 6_000)
    assert abs(b.cost_cny - 0.108) < 1e-9
    b.add(1_000, 100)
    # add 累计 tokens 并全量重算——种子不丢、单价一致时无缝衔接
    assert b.prompt_tokens == 31_000 and b.completion_tokens == 6_100
    assert abs(b.cost_cny - tokens_cost(31_000, 6_100)) < 1e-9


def test_engine_autoresume_restores_state(tmp_path, monkeypatch):
    """engine 端到端：同 job_id 二次 run 自动续检查点（步数/预算/消息链接续）。"""
    from secpt.runtime.engine import Engine, RunConfig

    monkeypatch.setenv("SECPT_VAR", str(tmp_path))  # autoresume 走 jobs_dir()
    _make_archive(tmp_path)

    class _TC:  # 与 test_engine_smoke 同构的假 tool_call
        def __init__(self, id, name, arguments):
            self.id = id
            self.function = type("F", (), {"name": name, "arguments": arguments})()

    class _Msg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Resp:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})()]
            self.usage = None

    class _LLM:
        def __init__(self):
            self.calls = 0

        async def call(self, messages, tools, model):
            self.calls += 1
            if self.calls == 1:
                return _Resp(
                    _Msg(
                        content="看看目录",
                        tool_calls=[_TC("c1", "bash", json.dumps({"command": "pwd"}))],
                    )
                )
            return _Resp(_Msg(content="ANSWER: FLAG{ok}"))

    cfg = RunConfig(
        job_id="jck",
        objective="测试任务全文",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=20,
        max_wall_s=60,
        budget_cost_cny=1.0,
        autoresume=True,  # 端到端验证自动恢复（顶层单任务语义）
    )
    result = asyncio.new_event_loop().run_until_complete(Engine(cfg, llm=_LLM()).run())

    assert result.status == "success"
    # 步数接续：检查点 9 步 + 本轮 2 步（不是从 0 重新数）
    assert result.steps == 11
    # 消息链接续：首条是存档 system（而非新链 "你是测试员。"）
    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == "sys"
    # 预算承接：种子 0.108 CNY + 本轮增量
    assert result.cost_cny >= 0.108
