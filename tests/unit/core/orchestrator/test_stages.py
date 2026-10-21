"""S6 多阶段折叠测试：profile.stages → frontier 线性 depends_on 意图链。

原 run_stages 编排器已退役（policy.DEPRECATED_MODULES），多阶段语义由
dag.run_task → run_frontier(stages=...) 承接：
- 每阶段 = 一条意图（role=阶段名，预算/工具/收尾条件映射意图字段）
- 阶段串行：depends_on 前一阶段 intent_id（deps_satisfied 确定性解锁）
- 交接：全链共用 asset_scope stage_key → M1 同资产 asset_state 产物搬运
- 中间阶段 ANSWER 是阶段交付物；stop_on_first_flag=False 保证全链跑完
"""

from __future__ import annotations

import asyncio
import json

import pytest

from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.orchestrator.dag import run_task


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk_llm(per_stage_calls: int = 1):
    """每阶段 per_stage_calls 次工具调用后 ANSWER；记录每个 engine 会话的 system。"""

    class _Fn:
        def __init__(self, n, a):
            self.name, self.arguments = n, a

    class _TC:
        def __init__(self, i):
            self.id = f"tc{i}"
            self.function = _Fn("bash", json.dumps({"command": "ls"}))

    class _Msg:
        def __init__(self, content, tcs):
            self.content = content
            self.tool_calls = tcs
            self.reasoning_content = None

    class _Resp:
        def __init__(self, msg):
            self.choices = [type("c", (), {"message": msg})()]

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.sessions: list[str] = []  # 每个 engine 会话的 system 首内容

        async def call(self, messages, tools, model):
            self.calls += 1
            tool_msgs = sum(1 for m in messages if m.get("role") == "tool")
            # 只认 engine 会话（system 首条；goal 拆解等 user 直调不计入）
            if messages[0].get("role") == "system" and tool_msgs == 0:
                self.sessions.append(messages[0]["content"])
            if tool_msgs < per_stage_calls:
                return _Resp(_Msg(f"step {self.calls}", [_TC(self.calls)]))
            return _Resp(_Msg(f"ANSWER: FLAG{{stage_{len(self.sessions)}}}", []))

    return FakeLLM()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    return tmp_path


class TestFoldedStages:
    def test_stages_run_in_order_as_intent_chain(self, env):
        """multistage profile → 三阶段按序执行，chains 角色即阶段名。"""
        llm = _mk_llm()
        spec = TaskSpec(
            job_id="ms1",
            task_type="multistage",
            objective="题目：拿下目标",
            budget=TaskBudget(),
        )
        summary = _run(run_task(spec, llm=llm))

        assert summary["mode"] == "frontier"
        assert [c["role"] for c in summary["chains"]] == ["recon", "getshell", "privesc"]
        assert all(c["status"] == "done" for c in summary["chains"])

        # 意图库：线性 depends_on 链 + 阶段预算/工具映射
        import sqlite3

        db = env / "jobs" / "ms1" / "frontier.sqlite"
        rows = (
            sqlite3.connect(db)
            .execute(
                "SELECT id,depends_on,role,max_steps,max_wall_s,tools FROM intents ORDER BY depth"
            )
            .fetchall()
        )
        assert [r[2] for r in rows] == ["recon", "getshell", "privesc"]
        assert rows[0][1] == ""  # 首阶段无依赖
        assert rows[1][1] == rows[0][0]  # depends_on = 前序 intent_id
        assert rows[2][1] == rows[1][0]
        assert [r[3] for r in rows] == [50, 120, 100]  # 阶段步数预算
        assert [r[4] for r in rows] == [900, 2400, 1800]  # 阶段墙钟预算
        assert "getshell" in (rows[1][5] or "")  # 阶段 tools 装配

    def test_handoff_carried_between_stages(self, env):
        """M1 同资产交接：后序阶段的 system 注入前序产物清单与结论。"""
        llm = _mk_llm()
        spec = TaskSpec(job_id="ms2", task_type="multistage", objective="题", budget=TaskBudget())
        _run(run_task(spec, llm=llm))

        assert len(llm.sessions) == 3
        assert "同资产前序工作交接" in llm.sessions[1]
        assert "产物已复制到本工作区" in llm.sessions[2]

    def test_middle_stage_answer_does_not_stop_chain(self, env):
        """中间阶段 ANSWER 是阶段交付物；stop_on_first_flag=False 保证全链跑完。"""
        llm = _mk_llm()
        spec = TaskSpec(job_id="ms3", task_type="multistage", objective="题", budget=TaskBudget())
        summary = _run(run_task(spec, llm=llm))

        assert len(summary["chains"]) == 3  # 全部阶段执行，未提前收敛
