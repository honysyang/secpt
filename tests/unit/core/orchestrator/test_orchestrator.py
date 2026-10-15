"""S5 Specialist 编排（团队模式）单测。

覆盖验收判据的核心：
- 单入口（N==1）→ 编排层休眠，直连单链（红线 2，无 recon / 无 warroom）
- 多入口（N>=2）→ 团队模式（frontier 角色意图编排，折叠 Specialist DAG）：
    - recon 意图先行 → exploit×N 意图依赖解锁并行认领
    - 角色意图挂靠资产（role/asset_scope），多 worker 认领互斥
- --no-orchestrator 对照 → 强制单链（红线 2）
- TaskSpec.entries 字段解析

RoleAwareFakeLLM：按 system 内容区分角色（recon / exploit），全程不碰真实 API；
flag 样式刻意用非固定格式（RCTF{...}），验证"AI 自评估、样式不固定"。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import ClassVar

import pytest
import yaml as _yaml

from secpt.core.job.models import TaskSpec
from secpt.core.job.store import JobStore
from secpt.core.orchestrator.dag import entries_of, needs_orchestrator, run_task

# 从 system 的工具说明里提取 work_dir（write_file 落盘目录）
_WORKDIR_RE = re.compile(r"写文件到 (.+?)/ 下")


# ---------------------------------------------------------------------------
# RoleAwareFakeLLM：按 system 内容扮演角色
# ---------------------------------------------------------------------------


class RoleAwareFakeLLM:
    """多角色假 LLM：recon 写 assets.json 后自宣告；exploit/单链 bash 后自宣告 flag。"""

    def __init__(self, n_bash: int = 1) -> None:
        self.n_bash = n_bash
        self.calls = 0

    async def call(self, messages, tools, model):
        self.calls += 1
        system = messages[0]["content"] if messages else ""
        # 只认角色阶段标记（【当前阶段：资产侦察…）——近期结果里的 recon 意图标题
        # 「资产侦察（recon）」不触发（避免 exploit 链误判为 recon）
        if "【当前阶段：资产侦察" in system:
            return self._recon_step(messages)
        # 团队模式 exploit 意图 / 单链：角色 stage 含"定向利用（exploit）"
        return self._exploit_step(messages)

    # ---------------- recon：写资产清单 → 自宣告 ----------------
    def _recon_step(self, messages):
        # 首次调用：把两条资产写进 ws/assets.json
        if not any(m.get("role") == "tool" for m in messages):
            work_dir = _WORKDIR_RE.search(messages[0]["content"]).group(1)
            content = json.dumps(
                [
                    {"host": "127.0.0.1", "port": 8001, "service": "http", "tech": "nginx"},
                    {"host": "127.0.0.1", "port": 8002, "service": "http", "tech": "flask"},
                ],
                ensure_ascii=False,
            )
            return _resp_assistant(
                tool_calls=[
                    _tc(
                        "recon-write",
                        "write_file",
                        {"path": f"{work_dir}/assets.json", "content": content},
                    )
                ]
            )
        return _resp_assistant(content="ANSWER: RECON_DONE")

    # ---------------- exploit / 通用单链：bash → 自宣告 flag ----------------
    def _exploit_step(self, messages):
        used_bash = sum(1 for m in messages if m.get("role") == "tool")
        if used_bash < self.n_bash:
            return _resp_assistant(tool_calls=[_tc("exploit-b", "bash", {"command": "echo probe"})])
        return _resp_assistant(content=f"ANSWER: RCTF{{single_{uuid.uuid4().hex[:6]}}}")


# ---------------- 模拟 OpenAI ChatCompletion 响应 ----------------


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, tc_id, name, arguments):
        self.id = tc_id
        self.type = "function"
        self.function = _Fn(name, json.dumps(arguments, ensure_ascii=False))


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None


class _Choice:
    def __init__(self, msg):
        self.message = msg


class _Resp:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]


def _resp_assistant(content="", tool_calls=None):
    return _Resp(_Msg(content=content, tool_calls=tool_calls))


def _tc(tc_id, name, arguments):
    return _TC(tc_id, name, arguments)


# ---------------------------------------------------------------------------
# 公共设施
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 jobs 目录 + 注入假 LLM 环境（不碰真实 API）。"""
    monkeypatch.setenv("SECPT_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    # run_engine 的 _api_key() 检查必须（单链分支），值本身不被使用
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    return tmp_path


def _spec(job_id: str, entries: list[str], task_type: str = "ctf") -> TaskSpec:
    return TaskSpec(
        job_id=job_id,
        task_type=task_type,
        objective="对给定入口做渗透测试，拿到目标证明（flag 样式不固定，由你评估）。",
        entries=list(entries),
    )


# ---------------------------------------------------------------------------
# 判据 4/5：单入口直连（编排层休眠）
# ---------------------------------------------------------------------------


async def test_single_entry_runs_single_chain(env) -> None:
    """N==1 → mode=single：无 recon、无 warroom 数据、无 finding 登记。"""
    spec = _spec("test-single", ["http://127.0.0.1:8001"])
    store = JobStore(str(env / "secpt.sqlite"))
    summary = await run_task(spec, store=store, llm=RoleAwareFakeLLM())

    assert summary["mode"] == "single(+retry)"  # 单链路径恒标重试装配
    assert summary["recon_assets"] == 0  # 没跑 recon
    assert summary["warroom_assets"] == 0  # 没建 warroom
    assert summary["warroom_attempts"] == 0
    assert summary["verified"] == 0
    assert len(summary["chains"]) == 1
    assert summary["chains"][0]["role"] == "exploit"
    assert len(summary["flags"]) == 1  # 单链自宣告一个 flag


# ---------------------------------------------------------------------------
# 判据 6：单链失败自动重试一次（新 job_id -r2 + retry_note，不复用 workspace）
# ---------------------------------------------------------------------------


async def test_single_chain_retries_on_failure(env, monkeypatch) -> None:
    """首轮 budget_exceeded 无 flag → 自动重试一次：新 job_id(-r2)、retry_note 追加、
    chains==2（attempt-1/attempt-2）、flags 来自第二轮、mode 仍 single(+retry)。"""
    from secpt.cli import engine_runner
    from secpt.runtime.engine import RunResult

    calls = {"n": 0}

    def fake_run_engine(**kwargs):  # run_single_chain 经 asyncio.to_thread 调用（同步函数）
        calls["n"] += 1
        objective = kwargs["objective"]
        if calls["n"] == 1:
            assert kwargs["job_id"] == "test-retry"
            assert "【上一次尝试未成功】" not in objective
            return RunResult(
                flag=None, steps=100, wall_s=50.0, status="budget_exceeded", messages=[]
            )
        # 第二次：必须换新 job_id（防污染 workspace）+ 追加 retry_note
        assert kwargs["job_id"] == "test-retry-r2"
        assert "【上一次尝试未成功】" in objective
        assert "不要把上次的步骤原样重跑一遍" in objective
        return RunResult(
            flag="RCTF{retry_got_it}", steps=10, wall_s=20.0, status="success", messages=[]
        )

    monkeypatch.setattr(engine_runner, "run_engine", fake_run_engine)

    spec = _spec("test-retry", ["http://127.0.0.1:8001"])
    store = JobStore(str(env / "secpt.sqlite"))
    summary = await run_task(spec, store=store, llm=RoleAwareFakeLLM())

    assert summary["mode"] == "single(+retry)"
    assert len(summary["chains"]) == 2  # attempt-1 / attempt-2
    assert summary["chains"][0]["status"] == "budget_exceeded"
    assert summary["chains"][0]["steps"] == 100
    assert summary["chains"][1]["status"] == "success"
    assert summary["chains"][1]["steps"] == 10
    assert summary["flags"] == ["RCTF{retry_got_it}"]  # 只有第二轮产出 flag
    assert summary["wall_s"] == 70.0  # 50.0 + 20.0 累加
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 判据 1/2/3：多入口并行 DAG
# ---------------------------------------------------------------------------


async def test_multi_entry_runs_team_mode(env) -> None:
    """N==2 → mode=team：recon 意图先行，exploit×N 依赖解锁并行认领，每资产挂靠。"""
    spec = _spec("test-dag", ["http://127.0.0.1:8001", "http://127.0.0.1:8002"])
    store = JobStore(str(env / "secpt.sqlite"))
    summary = await run_task(spec, store=store, llm=RoleAwareFakeLLM(), use_orchestrator=True)

    assert summary["mode"] == "team"
    chains = summary["chains"]
    assert len(chains) >= 3  # recon 1 + exploit×2
    roles = [c["role"] for c in chains]
    assert roles.count("recon") == 1
    assert roles.count("exploit") == 2
    # recon 意图先结算为 done（侦察完成即解锁 exploit），exploit 挂靠各自资产
    recon = next(c for c in chains if c["role"] == "recon")
    assert recon["status"] == "done"
    assert {c["asset_scope"] for c in chains if c["role"] == "exploit"} == {
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8002",
    }
    assert all(c["status"] == "done" for c in chains if c["role"] == "exploit")
    assert len(summary["flags"]) == 2  # 每条 exploit 各自宣告一个 flag

    # F3 回归：summary 契约完整（cmd_run 直接/防御读取这些键；此前缺键
    # 导致 `secpt run --mode dag` 跑完成功后报告路径 KeyError: 'entries' 崩溃）
    assert summary["entries"] == ["http://127.0.0.1:8001", "http://127.0.0.1:8002"]
    for k in ("warroom_assets", "warroom_attempts", "findings", "verified"):
        assert k in summary, k


# ---------------------------------------------------------------------------
# 判据 5：--no-orchestrator 对照
# ---------------------------------------------------------------------------


async def test_no_orchestrator_falls_back_to_single(env) -> None:
    """多入口 + use_orchestrator=False → 强制单链（对照分支）。"""
    spec = _spec("test-noorch", ["http://127.0.0.1:8001", "http://127.0.0.1:8002"])
    store = JobStore(str(env / "secpt.sqlite"))
    summary = await run_task(spec, store=store, llm=RoleAwareFakeLLM(), use_orchestrator=False)

    assert summary["mode"] == "single(+retry)"  # 非 dag 一律标 single(+retry)
    assert summary["verified"] == 0
    assert summary["warroom_assets"] == 0
    assert len(summary["flags"]) <= 1  # 单链只出一个 flag
    assert len(summary["chains"]) == 1


async def test_team_discovers_more_than_single(env) -> None:
    """红线 2 对照：同样多入口，团队模式 exploit×N 发现 >= 单链。"""
    store = JobStore(str(env / "secpt.sqlite"))
    team = await run_task(
        _spec("test-contrast-dag", ["http://127.0.0.1:8001", "http://127.0.0.1:8002"]),
        store=store,
        llm=RoleAwareFakeLLM(),
        use_orchestrator=True,
    )
    single = await run_task(
        _spec("test-contrast-single", ["http://127.0.0.1:8001", "http://127.0.0.1:8002"]),
        store=store,
        llm=RoleAwareFakeLLM(),
        use_orchestrator=False,
    )

    assert team["mode"] == "team" and single["mode"] == "single(+retry)"
    assert len(team["flags"]) >= len(single["flags"])  # 双资产并行 exploit ≥ 单链
    assert len([c for c in team["chains"] if c["role"] == "exploit"]) >= 2


# ---------------------------------------------------------------------------
# TaskSpec.entries 字段解析
# ---------------------------------------------------------------------------


def test_taskspec_entries_parsing() -> None:
    """entries >= 2 → 需要编排；空 / 单入口 → 直连单链。"""
    doc = _yaml.safe_load(
        "job_id: spec-test\n"
        "task_type: ctf\n"
        "objective: 测试多入口\n"
        "entries:\n"
        "  - http://10.0.0.1:8000\n"
        "  - http://10.0.0.2:9000\n"
    )
    spec = TaskSpec.model_validate(doc)
    assert entries_of(spec) == ["http://10.0.0.1:8000", "http://10.0.0.2:9000"]
    # 对照组结论：编排层默认关闭，仅显式开启才启用
    assert needs_orchestrator(spec) is False
    assert needs_orchestrator(spec, use_dag=True) is True

    single = _spec("spec-single", ["http://10.0.0.1:8000"])
    assert needs_orchestrator(single) is False
    assert needs_orchestrator(single, use_dag=True) is False  # 单入口永远不编排

    empty = _spec("spec-empty", [])
    assert needs_orchestrator(empty) is False


class TestRetryInheritsAnswers:
    def test_retry_note_carries_previous_answers(self, tmp_path, monkeypatch):
        """懒升级 r2 必须继承 r1 已产出的 ANSWER（benchmark 续跑不重打已通题）。"""
        import asyncio as _a

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.job.models import TaskBudget, TaskSpec
        from secpt.core.orchestrator.dag import run_single_chain

        class _R1:
            flag = None
            steps = 100
            wall_s = 600.0
            status = "step_exceeded"
            messages: ClassVar[list] = [
                {"role": "assistant", "content": "ANSWER: 已通关 xben-005/006，flag{a} flag{b}"},
            ]

        class _R2:
            flag = None
            steps = 1
            wall_s = 1.0
            status = "failed"
            messages: ClassVar[list] = []

        calls = []

        def fake_run_engine(**kw):
            calls.append(kw)
            return _R1() if len(calls) == 1 else _R2()

        import secpt.cli.engine_runner as er

        orig = er.run_engine
        er.run_engine = fake_run_engine
        try:
            spec = TaskSpec(job_id="rt1", objective="跑分", budget=TaskBudget(steps=1))
            _a.new_event_loop().run_until_complete(run_single_chain(spec, llm=object()))
        finally:
            er.run_engine = orig
        # r2 的 objective 必须包含 r1 的成果
        assert len(calls) == 2
        assert "flag{a}" in calls[1]["objective"]
        assert "上一轮已产出的成果" in calls[1]["objective"]
