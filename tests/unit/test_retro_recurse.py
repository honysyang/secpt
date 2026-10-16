"""复盘会 retro() + 失败驱动局部递归（设计 docs/设计-工作循环形态.md §4.1/4.2，P3 验收）。

验收点：
- retro() 三态路由（converge/recurse/continue）纯确定性：全部 SQL/计数、零 LLM；
- recurse 触发条件严格：复核后仍败（尝试 2 次）+ 路线型失败（step_exceeded/
  wallclock）+ 未展开过 + 资产无 confirmed finding；
- _expand_recurse：产 recon+exploit 双子意图（依赖链）、标记父意图（每父最多一层）、
  尊重 max_intents 上限；
- 集成：父意图两次 step_exceeded  exhausted 后，主循环自动展开子意图并由
  worker 在子意图上拿到 flag 收敛。
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from secpt.core.job.fakellm import _resp
from secpt.core.orchestrator.frontier import (
    Frontier,
    _expand_recurse,
    retro,
    run_frontier,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _exhausted_route_failed(fr: Frontier, title: str = "失败父意图", attempts: int = 2) -> str:
    iid = fr.add(title, goal="g", done_criteria="d", asset_scope="http://t/x")
    fr.settle(
        iid,
        status="exhausted",
        result="steps=30",
        fail_reason=f"step_exceeded（尝试 {attempts} 次）",
    )
    return iid


class TestRetroPhases:
    def test_converge_when_subgoals_settled(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        a = fr.add("子目标1", subgoal="s1")
        fr.settle(a, status="done", result="ok")
        assert retro(fr) == "converge"

    def test_recurse_on_route_failure_after_recheck(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        _exhausted_route_failed(fr)
        assert retro(fr) == "recurse"

    def test_continue_when_not_route_failure(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        iid = fr.add("错误父意图", asset_scope="http://t/x")
        fr.settle(iid, status="exhausted", result="x", fail_reason="error（尝试 2 次）")
        assert retro(fr) == "continue"

    def test_recurse_on_hard_budget_stop(self, tmp_path):
        """意图级步数预算硬止损（无复核轮、一次即终态）也算路线型失败——
        跑分 exhausted 题群正是此形态，是递归要救的主要人群。"""
        fr = Frontier(tmp_path / "f.db")
        iid = fr.add("预算耗尽题", asset_scope="http://t/y", max_steps=30)
        fr.settle(
            iid, status="exhausted", result="steps=30", fail_reason="超意图步数预算（30 步硬止损）"
        )
        assert retro(fr) == "recurse"

    def test_continue_when_first_attempt_only(self, tmp_path):
        """仅首轮失败（尝试 1 次，尚未复核）不触发递归——复核是递归的前置。"""
        fr = Frontier(tmp_path / "f.db")
        _exhausted_route_failed(fr, attempts=1)
        assert retro(fr) == "continue"

    def test_continue_when_already_expanded(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        pid = _exhausted_route_failed(fr)
        assert _expand_recurse(fr, base="", max_intents=10) == 2
        # 父意图已标记展开 → retro 不再 recurse（只剩 open 子意图，无新候选）
        assert retro(fr) == "continue"
        with fr._conn() as c:
            why = c.execute("SELECT fail_reason FROM intents WHERE id=?", (pid,)).fetchone()[0]
        assert "已递归展开" in why


class TestExpandRecurse:
    def test_creates_dependent_pair_and_marks_parent(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        pid = _exhausted_route_failed(fr)
        assert _expand_recurse(fr, base="", max_intents=10) == 2
        rows = fr.list_all()
        new = [r for r in rows if r.get("source") == "retro_recurse"]
        assert len(new) == 2
        recon = next(r for r in new if r.get("role") == "recon")
        exploit = next(r for r in new if r.get("role") == "exploit")
        assert exploit["depends_on"] == recon["id"]  # 依赖链：exploit 等 recon
        assert recon["depth"] == 1 and exploit["depth"] == 1
        # 每父意图最多一层：二次展开返回 0
        assert _expand_recurse(fr, base="", max_intents=10) == 0
        with fr._conn() as c:
            why = c.execute("SELECT fail_reason FROM intents WHERE id=?", (pid,)).fetchone()[0]
        assert "已递归展开" in why

    def test_respects_max_intents(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        _exhausted_route_failed(fr)
        assert _expand_recurse(fr, base="", max_intents=2) == 0  # 1 父 + 2 子 > 上限
        assert fr.count_open() == 0


class _EmptyPlanner:
    async def call(self, messages, tools, model):
        class _Msg:
            content = "[]"

        class _Resp:
            choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

        return _Resp()


class _FailThenWinLLM:
    """父意图：静态空转 step_exceeded；子意图（换路线再打）：直接拿 flag。"""

    async def call(self, messages, tools, model):
        system = str(messages[0].get("content") or "")
        if "换路线再打" in system:
            return _resp({"role": "assistant", "content": "ANSWER: FLAG{recurse_win}"})
        return _resp(
            {
                "role": "assistant",
                "content": "static",
                "tool_calls": [
                    {
                        "id": "x1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})},
                    }
                ],
            }
        )


class TestRecurseIntegration:
    def test_main_loop_expands_and_wins_on_sub_intent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_LOOP_STOP", "0")  # 本测只验递归路由，关掉止损状态机
        fr = Frontier(tmp_path / "fr.db")
        fr.add(
            "打目标",
            goal="拿 flag",
            done_criteria="ANSWER flag",
            priority=9,
            max_steps=3,  # 低预算 → 两次尝试均 step_exceeded → exhausted（尝试 2 次）
        )
        summary = _run(
            run_frontier(
                "目标：拿下 recurse 测试",
                llm=_FailThenWinLLM(),
                planner_llm=_EmptyPlanner(),
                base_job_id="rc1",
                max_rounds=12,
                frontier=fr,
                max_intents=10,
            )
        )
        assert "FLAG{recurse_win}" in summary["flags"]
        sources = {c.get("source") for c in summary["chains"]}
        assert "retro_recurse" in sources  # 子意图确实由复盘会展开
        statuses = {c["intent"]: c["status"] for c in summary["chains"]}
        assert any(s == "done" for t, s in statuses.items() if "换路线再打" in t)
