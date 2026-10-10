"""多阶段编排测试：recon → getshell 串行、共享 workspace、交接注入、提前终止。"""

from __future__ import annotations

import asyncio
import json

from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.orchestrator.stages import run_stages
from secpt.core.profile.loader import Budget, Profile, Stage


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _profile(stage_defs: list[dict]) -> Profile:
    return Profile(
        name="multistage",
        tools=["bash"],
        stages=[Stage(**s) for s in stage_defs],
        budget=Budget(steps=10, wall_s=300),
    )


def _mk_llm(final_answer: str, per_stage_calls: int = 2):
    """每阶段 per_stage_calls 次工具调用后 ANSWER。"""

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
            self.stages_seen = []

        async def call(self, messages, tools, model):
            self.calls += 1
            self.stages_seen.append(messages[0]["content"])
            if self.calls % per_stage_calls == 0:
                return _Resp(_Msg(f"ANSWER: {final_answer}", []))
            return _Resp(_Msg(f"step {self.calls}", [_TC(self.calls)]))

    return FakeLLM()


class TestStageRunner:
    def test_two_stages_run_in_order_with_shared_ws(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        llm = _mk_llm("SHELL_pwned_stage2", per_stage_calls=2)
        prof = _profile(
            [
                {
                    "name": "recon",
                    "role": "侦察员",
                    "stop_condition": "输出侦察摘要",
                    "budget": {"steps": 6, "wall_s": 60},
                },
                {"name": "getshell", "role": "利用执行者", "budget": {"steps": 6, "wall_s": 60}},
            ]
        )
        spec = TaskSpec(
            job_id="ms1", task_type="multistage", objective="题目：拿下目标", budget=TaskBudget()
        )
        summary = _run(run_stages(spec, prof, llm=llm))
        assert summary["mode"] == "stages"
        assert [c["role"] for c in summary["chains"]] == ["recon", "getshell"]
        assert summary["flags"] == ["SHELL_pwned_stage2"]
        # 共享 workspace：两个阶段同一 work_dir
        base = tmp_path / "jobs" / "ms1"
        assert (base / "ws").is_dir()
        for stage in ("recon", "getshell"):
            assert (tmp_path / "jobs" / f"ms1-{stage}" / "task.yaml").is_file()

    def test_handoff_injected_into_next_stage(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        llm = _mk_llm("done", per_stage_calls=1)
        prof = _profile(
            [
                {"name": "recon", "role": "侦察"},
                {"name": "getshell", "role": "利用"},
            ]
        )
        spec = TaskSpec(job_id="ms2", task_type="multistage", objective="题", budget=TaskBudget())
        _run(run_stages(spec, prof, llm=llm))
        second_stage_system = llm.stages_seen[1]
        assert "上一阶段" in second_stage_system
        assert "共享工作目录" in second_stage_system

    def test_middle_stage_answer_is_handoff_not_final(self, tmp_path, monkeypatch):
        """中间阶段的 ANSWER 是阶段交付物（注入交接），不提前终止编排；
        只有最终阶段的 ANSWER 才是整体成果。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        llm = _mk_llm("FLAG{from_final}", per_stage_calls=1)
        prof = _profile(
            [
                {"name": "recon", "role": "侦察"},
                {"name": "getshell", "role": "利用"},
                {"name": "privesc", "role": "提权"},
            ]
        )
        spec = TaskSpec(job_id="ms3", task_type="multistage", objective="题", budget=TaskBudget())
        summary = _run(run_stages(spec, prof, llm=llm))
        assert len(summary["chains"]) == 3  # 全部阶段执行
        assert summary["flags"] == ["FLAG{from_final}"]

    def test_stage_budget_fallback_to_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        llm = _mk_llm("x", per_stage_calls=1)
        prof = _profile([{"name": "only", "role": "r"}])  # 无阶段预算
        spec = TaskSpec(job_id="ms4", task_type="multistage", objective="题", budget=TaskBudget())
        summary = _run(run_stages(spec, prof, llm=llm))
        assert summary["chains"][0]["status"] == "success"
