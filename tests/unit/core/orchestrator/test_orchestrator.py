"""S6 统一执行路由单测：run_task 唯一入口（benchmark → campaign；其余 → frontier）。

覆盖验收判据：
- 单入口 → frontier planner 驱动（stop_on_first_flag，拿到首个 flag 即收敛）
- 多入口 → frontier role_plan 角色骨架（recon 先行 → exploit×N 依赖解锁并行认领）
- profile.stages 非空 → 折叠为线性 depends_on 意图链（清单型，关闭 planner）
- spec.budget 透传：steps → default_max_steps；wall_s>0 → deadline_ts
- benchmark → campaign（不经 frontier）
- augment_objective 拼接与 entries 维度补齐

RoleAwareFakeLLM：按 system 内容区分角色（recon / exploit），全程不碰真实 API；
flag 样式刻意用非固定格式（RCTF{...}），验证"AI 自评估、样式不固定"。
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.job.store import JobStore
from secpt.core.orchestrator.dag import run_task

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

    # ---------------- exploit / 通用链：bash → 自宣告 flag ----------------
    def _exploit_step(self, messages):
        used_bash = sum(1 for m in messages if m.get("role") == "tool")
        if used_bash < self.n_bash:
            return _resp_assistant(tool_calls=[_tc("exploit-b", "bash", {"command": "echo probe"})])
        return _resp_assistant(content=f"ANSWER: RCTF{{single_{uuid.uuid4().hex[:6]}}}")


# ---------------- 模拟 OpenAI ChatCompletion 响应 ----------------


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


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
    # engine 的 _api_key() 检查必须，值本身不被使用
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    return tmp_path


def _spec(
    job_id: str, entries: list[str], task_type: str = "ctf", budget: TaskBudget | None = None
) -> TaskSpec:
    return TaskSpec(
        job_id=job_id,
        task_type=task_type,
        objective="对给定入口做渗透测试，拿到目标证明（flag 样式不固定，由你评估）。",
        entries=list(entries),
        budget=budget or TaskBudget(),
    )


def _capture_frontier(monkeypatch) -> dict:
    """拦截 run_frontier，记录调用参数（路由判定的确定性测试用具）。"""

    captured: dict = {}

    async def fake_run_frontier(objective, **kw):
        captured["objective"] = objective
        captured.update(kw)
        return {"mode": "frontier", "flags": [], "chains": [], "wall_s": 0.0}

    monkeypatch.setattr("secpt.core.orchestrator.frontier.run_frontier", fake_run_frontier)
    return captured


# ---------------------------------------------------------------------------
# 路由判定（frontier 调用参数）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_entry_planner_driven(env, monkeypatch) -> None:
    """N==1 → frontier planner 驱动：stop_on_first_flag，workers=profile 默认。"""
    captured = _capture_frontier(monkeypatch)
    spec = _spec("r-single", ["http://127.0.0.1:8001"])
    await run_task(spec, llm=RoleAwareFakeLLM())

    assert captured["role_plan"] is False
    assert captured["stop_on_first_flag"] is True
    assert captured["max_workers"] == 1  # ctf profile 未声明 workers
    assert "【目标入口】" in captured["objective"]  # augment_objective 拼接
    assert captured["default_assets"] == ["http://127.0.0.1:8001"]


@pytest.mark.asyncio
async def test_multi_entry_generic_plan(env, monkeypatch) -> None:
    """N==2 → 通用独立任务（v3：不再预置角色骨架，交付/后勤通道开启）。"""
    captured = _capture_frontier(monkeypatch)
    spec = _spec("r-multi", ["http://127.0.0.1:8001", "http://127.0.0.1:8002"])
    await run_task(spec, llm=RoleAwareFakeLLM())

    assert captured["role_plan"] is False
    assert captured["stop_on_first_flag"] is False
    assert captured["enable_delivery"] is True
    assert captured["enable_logistics"] is True


@pytest.mark.asyncio
async def test_stages_profile_folds_to_intent_chain(env, monkeypatch) -> None:
    """profile.stages 非空（multistage）→ 折叠参数：stages + 关闭 planner + 串行。"""
    captured = _capture_frontier(monkeypatch)
    spec = _spec("r-stages", [], task_type="multistage")
    await run_task(spec, llm=RoleAwareFakeLLM())

    stages = captured.get("stages") or []
    assert len(stages) == 3  # recon / getshell / privesc
    assert captured["max_intents"] == 3  # planner 关闭（清单型语义）
    assert captured["stop_on_first_flag"] is False  # 全阶段跑完
    assert captured["max_workers"] == 1  # depends_on 链串行


@pytest.mark.asyncio
async def test_budget_steps_and_wall_passed(env, monkeypatch) -> None:
    """spec.budget 透传：steps → default_max_steps；wall_s>0 → deadline_ts。"""
    captured = _capture_frontier(monkeypatch)
    spec = _spec("r-budget", ["http://127.0.0.1:8001"], budget=TaskBudget(steps=42, wall_s=600.0))
    await run_task(spec, llm=RoleAwareFakeLLM())

    assert captured["default_max_steps"] == 42
    assert captured["deadline_ts"] is not None


@pytest.mark.asyncio
async def test_benchmark_routes_to_campaign(env, monkeypatch) -> None:
    """benchmark → campaign（不经 frontier）；entries 维度补齐。"""
    calls: list[str] = []

    async def fake_campaign(spec, **kw):
        calls.append(spec.job_id)
        return {"mode": "benchmark(parallel)", "flags": [], "chains": [], "wall_s": 0.0}

    monkeypatch.setattr("secpt.core.orchestrator.benchmark.run_benchmark_campaign", fake_campaign)
    _capture_frontier(monkeypatch)  # frontier 不应被调用

    spec = _spec("r-bench", ["http://x"], task_type="benchmark")
    summary = await run_task(spec, llm=RoleAwareFakeLLM())

    assert calls == ["r-bench"]
    assert summary["entries"] == ["http://x"]


# ---------------------------------------------------------------------------
# 集成：单入口 frontier 全环（planner 产意图 → worker 执行 → 收敛）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_entry_full_frontier_loop(env, monkeypatch) -> None:
    """N==1 集成：planner 产任务 → 执行 → 首个 flag 收敛（交付门关闭，专测主循环）。"""
    from secpt.core.orchestrator import frontier as _fr

    async def fake_plan_round(objective, fr, planner_llm, **kw):
        fr.add(
            "利用 http://127.0.0.1:8001",
            goal="针对 http://127.0.0.1:8001 做定向利用",
            done_criteria="拿到 flag 或确认漏洞",
            asset_scope="http://127.0.0.1:8001",
        )
        return 1

    monkeypatch.setattr(_fr, "plan_round", fake_plan_round)
    monkeypatch.setenv("SECPT_ORCH_DELIVERY", "0")
    monkeypatch.setenv("SECPT_ORCH_LOGISTICS", "0")
    monkeypatch.setenv("SECPT_ORCH_COMMANDER", "0")  # 本测专测 v3 planner 路径

    spec = _spec("test-single", ["http://127.0.0.1:8001"])
    store = JobStore(str(env / "secpt.sqlite"))
    summary = await run_task(spec, store=store, llm=RoleAwareFakeLLM())

    assert summary["mode"] == "frontier"
    assert len(summary["chains"]) == 1
    assert len(summary["flags"]) == 1  # 首个 flag 即收敛（stop_on_first_flag）


# ---------------------------------------------------------------------------
# 集成：多入口通用任务（v3：独立任务并行/顺序执行，无角色依赖）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_entry_runs_generic_tasks(env, monkeypatch) -> None:
    """N==2：planner 产两条独立通用任务（无 role/depends_on），各自执行。"""
    from secpt.core.orchestrator import frontier as _fr

    added = False

    async def fake_plan_round(objective, fr, planner_llm, **kw):
        nonlocal added
        if added:
            return 0
        added = True
        for asset in ("http://127.0.0.1:8001", "http://127.0.0.1:8002"):
            fr.add(
                f"测试 {asset}",
                goal=f"针对 {asset} 找可验证成果",
                done_criteria="拿到 flag 或确认漏洞",
                asset_scope=asset,
            )
        return 2

    monkeypatch.setattr(_fr, "plan_round", fake_plan_round)
    monkeypatch.setenv("SECPT_ORCH_DELIVERY", "0")
    monkeypatch.setenv("SECPT_ORCH_LOGISTICS", "0")
    monkeypatch.setenv("SECPT_ORCH_COMMANDER", "0")  # 本测专测 v3 planner 路径

    spec = _spec("test-dag", ["http://127.0.0.1:8001", "http://127.0.0.1:8002"])
    store = JobStore(str(env / "secpt.sqlite"))
    summary = await run_task(spec, store=store, llm=RoleAwareFakeLLM())

    assert summary["mode"] == "frontier"
    chains = summary["chains"]
    assert len(chains) == 2
    assert all(c["role"] == "" for c in chains)  # 不再有角色模板
    assert {c["asset_scope"] for c in chains} == {
        "http://127.0.0.1:8001",
        "http://127.0.0.1:8002",
    }
    assert len(summary["flags"]) == 2  # 多资产持续跑完，不因首个 flag 收敛
    assert all(c["status"] == "done" for c in chains if c["role"] == "exploit")
    assert len(summary["flags"]) == 2  # 每条 exploit 各自宣告一个 flag

    # F3 回归：summary 契约完整（cmd_run 直接/防御读取这些键；此前缺键
    # 导致 `secpt run` 跑完成功后报告路径 KeyError: 'entries' 崩溃）
    assert summary["entries"] == ["http://127.0.0.1:8001", "http://127.0.0.1:8002"]
    for k in ("warroom_assets", "warroom_attempts", "findings", "verified"):
        assert k in summary, k
