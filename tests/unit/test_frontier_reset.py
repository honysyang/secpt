"""RESET（设计 docs/设计-工作循环形态.md §3，P2 验收）测试。

验收点：
- 三档止损 stalled + ws 有实质产出 → 编排层自动 RESET：新 -rs 子 job 换会话重建，
  handoff 四件套（摘要/产物清单/否定清单/原目标）注入，模型换路线拿 flag → done；
- ws 无产出（纯空转）→ 不 RESET，直接 exhausted（空转不续语义）；
- _reset_handoff_summary LLM 失败时退化为 transcript 尾部（确定性兜底）。
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from secpt.core.job.fakellm import _resp
from secpt.core.orchestrator.frontier import (
    Frontier,
    _reset_handoff_summary,
    _reset_negative_list,
    run_frontier,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _EmptyPlanner:
    async def call(self, messages, tools, model):
        class _Msg:
            content = "[]"

        class _Resp:
            choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

        return _Resp()


class _StallThenRecoverLLM:
    """首轮：写产出文件后静态空转至 stalled；看到 RESET 注入后换路线拿 flag。"""

    def __init__(self, recover_flag: str | None = "FLAG{reset_ok}", write_output: bool = True):
        self.calls = 0
        self.recover_flag = recover_flag
        self.write_output = write_output

    async def call(self, messages, tools, model):
        self.calls += 1
        if self.recover_flag and any("RESET 重建" in str(m.get("content") or "") for m in messages):
            return _resp({"role": "assistant", "content": f"ANSWER: {self.recover_flag}"})
        if self.write_output:
            # 每步幂等写产出（预引擎阶段也会消耗若干次调用，无法依赖计数/一次写；
            # bash 重定向被治理链拦截，走 write_file 工具）
            return _resp(
                {
                    "role": "assistant",
                    "content": f"step {self.calls}",
                    "tool_calls": [
                        {
                            "id": f"s{self.calls}",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "probe.txt", "content": "v=1.0"}),
                            },
                        }
                    ],
                }
            )
        return _resp(
            {
                "role": "assistant",
                "content": f"step {self.calls}",
                "tool_calls": [
                    {
                        "id": f"s{self.calls}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})},
                    }
                ],
            }
        )


def _env_fast(monkeypatch):
    monkeypatch.setenv("SECPT_LOOP_STOP", "1")
    monkeypatch.setenv("SECPT_WRAP_AFTER_NUDGE", "2")
    monkeypatch.setenv("SECPT_STALL_AFTER_WRAP", "2")
    monkeypatch.setenv("SECPT_WRAP_AT_PCT", "200")  # 关掉步数收尾腿，专注 nudge→wrap→stall


class TestReset:
    def test_stalled_with_ws_output_resets_and_recovers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        _env_fast(monkeypatch)
        fr = Frontier(tmp_path / "fr.db")
        fr.add(
            "打卡住题",
            goal="拿到 flag",
            done_criteria="ANSWER flag",
            priority=9,
            max_steps=60,
        )
        summary = _run(
            run_frontier(
                "目标：拿下 reset 测试",
                llm=_StallThenRecoverLLM(),
                planner_llm=_EmptyPlanner(),
                base_job_id="rst1",
                max_rounds=8,
                frontier=fr,
                max_intents=1,
            )
        )
        assert summary["flags"] == ["FLAG{reset_ok}"]
        # RESET 换会话：存在 -rs 子 job（独立事件流/工作区）
        assert any(str(j).endswith("-rs") for j in summary.get("sub_jobs", []))
        statuses = {c["intent"]: c["status"] for c in summary["chains"]}
        assert statuses.get("打卡住题") == "done"

    def test_stalled_without_ws_output_goes_exhausted(self, tmp_path, monkeypatch):
        """纯空转（无任何 ws 产出）→ 不重建，直接 exhausted（设计 §3.1 门）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        _env_fast(monkeypatch)
        fr = Frontier(tmp_path / "fr.db")
        fr.add("空转题", goal="拿 flag", done_criteria="ANSWER flag", priority=9, max_steps=60)
        summary = _run(
            run_frontier(
                "目标：拿下 reset 测试",
                llm=_StallThenRecoverLLM(recover_flag=None, write_output=False),  # 纯空转永不回答
                planner_llm=_EmptyPlanner(),
                base_job_id="rst2",
                max_rounds=8,
                frontier=fr,
                max_intents=1,
            )
        )
        assert summary["flags"] == []
        assert not any(str(j).endswith("-rs") for j in summary.get("sub_jobs", []))
        statuses = {c["intent"]: c["status"] for c in summary["chains"]}
        assert statuses.get("空转题") == "exhausted"


class TestResetHandoffPieces:
    def test_handoff_summary_falls_back_to_tail(self):
        """LLM 不可用时退化为最后一条 assistant 正文尾部（确定性兜底）。"""
        result = type(
            "R",
            (),
            {
                "messages": [
                    {"role": "assistant", "content": "早期分析"},
                    {"role": "tool", "content": "x"},
                    {"role": "assistant", "content": "最后结论：union 注入被 WAF 拦截"},
                ]
            },
        )()
        out = _run(_reset_handoff_summary(result, "obj", llm=None))
        assert "union 注入被 WAF 拦截" in out

    def test_negative_list_aggregates_exhausted(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        iid = fr.add("失败路线A", goal="g", done_criteria="d")
        fr.settle(
            iid, status="exhausted", result="steps=10", fail_reason="step_exceeded（尝试 2 次）"
        )
        out = _reset_negative_list(fr, None)
        assert "失败路线A" in out and "step_exceeded" in out
