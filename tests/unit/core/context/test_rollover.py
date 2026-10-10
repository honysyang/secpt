"""test_rollover：handoff 能作为新链起点继续推进；累计步数不重置。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from secpt.core.context.roundpool import build_new_chain, should_rollover
from secpt.core.context.summarize import StubSummarizer
from secpt.runtime.engine import Engine, RunConfig
from secpt.runtime.llm.client import sanitize_messages


def test_should_rollover_triggers() -> None:
    # 步数触发
    assert should_rollover(steps_in_round=60, wall_in_round=10, est_tokens=100,
                           window=100_000)[0]
    # 墙钟触发
    assert should_rollover(steps_in_round=10, wall_in_round=1500, est_tokens=100,
                           window=100_000)[0]
    # 窗口触发
    assert should_rollover(steps_in_round=10, wall_in_round=10, est_tokens=100,
                           window=100)[0]
    # 子目标完成
    assert should_rollover(steps_in_round=1, wall_in_round=1, est_tokens=1,
                           window=100_000, subtask_done=True)[0]
    # 都不触发
    assert should_rollover(steps_in_round=10, wall_in_round=10, est_tokens=100,
                           window=100_000)[0] is False


def test_build_new_chain_handoff_is_start_point(tmp_path: Path) -> None:
    system = {"role": "system", "content": "你是{角色}。【任务全文】...objective...先验..."}
    handoff = asyncio.run(StubSummarizer()("objective", []))
    new_chain = build_new_chain(system, handoff)

    assert len(new_chain) == 2
    assert new_chain[0]["role"] == "system"
    assert new_chain[0]["content"] == system["content"]   # system 原样保留
    assert new_chain[1]["role"] == "user"
    assert new_chain[1]["content"].startswith("【上一回合交接】")
    # 六段齐全（含"已排除的尝试"，防重复踩坑）
    for seg in ("1. 任务目标", "2. 已确认事实", "3. 已获取凭据",
                "4. 已排除的尝试", "5. 当前进展", "6. 下一步候选"):
        assert seg in handoff
    # 作为新链起点：无孤立结构，模拟请求不报错
    sanitize_messages(new_chain)


class _RolloverLLM:
    """假 LLM：第一步沿 handoff 链继续调工具，第二步出 flag。"""

    def __init__(self, work_dir: Path) -> None:
        self.calls = 0
        self.work_dir = work_dir

    async def call(self, messages, tools, model):
        self.calls += 1
        if self.calls == 1:
            target = self.work_dir / "flag.txt"
            args = json.dumps({"path": str(target), "content": "FLAG{rollover_ok}"})
            return _resp({
                "role": "assistant", "content": "继续推进。",
                "tool_calls": [{"id": "r1", "type": "function",
                                "function": {"name": "write_file", "arguments": args}}],
            })
        return _resp({"role": "assistant", "content": "ANSWER: FLAG{rollover_ok}"})


def _resp(msg_dict: dict):
    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _TC:
        def __init__(self, tc):
            self.id = tc["id"]
            self.function = _Fn(tc["function"]["name"], tc["function"]["arguments"])

    class _Msg:
        def __init__(self, d):
            self.content = d.get("content") or ""
            self.tool_calls = [_TC(x) for x in (d.get("tool_calls") or [])]
            self.reasoning_content = None

    class _Resp:
        def __init__(self, msg):
            self.choices = [type("c", (), {"message": _Msg(msg)})()]

    return _Resp(msg_dict)


async def test_rollover_chain_continues(tmp_path: Path) -> None:
    """handoff 新链 + Engine 能继续推进并拿到 flag（S3 验收判据 5）。"""
    llm = _RolloverLLM(tmp_path / "ws")
    cfg = RunConfig(
        job_id="rollover-test",
        objective="目标：写 flag 文件",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=10,
        max_wall_s=60,
    )
    # 模拟"上一回合交接"作为初始链（首条 system + user handoff）
    handoff = await StubSummarizer()("目标：写 flag 文件", [])
    cfg.initial_messages = build_new_chain(
        {"role": "system", "content": "你是测试员。"}, handoff
    )
    result = await Engine(cfg, llm=llm).run()
    assert result.status == "success"
    assert result.flag == "FLAG{rollover_ok}"
    # 新链起点上继续跑，没重头乱来：首条 system + user 交接，然后直接工具调用
    assert result.messages[0]["role"] == "system"
    assert result.messages[1]["role"] == "user"
    assert result.messages[1]["content"].startswith("【上一回合交接】")
    assert result.messages[2]["role"] == "assistant"
