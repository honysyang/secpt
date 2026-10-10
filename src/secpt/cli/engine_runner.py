"""CLI 层引擎执行器：把 Engine 跑起来并接上 S1 持久化。

统一入口：
- ``run_engine(...)``      —— 新建任务跑一次（secpt run / chat 复用）
- ``resume_engine(job_id)``—— 从磁盘断点继续（secpt resume）

职责：
- 写 var/jobs/<job_id>/task.yaml 快照
- 挂 EventSink hooks（events.jsonl + SQLite 双写）
- 每轮落盘 rounds/NNN.messages.json（on_round_end）
- 结束更新 JobRecord 状态
"""

from __future__ import annotations

import asyncio
import os
import uuid

from secpt.core.job.event import EventSink
from secpt.core.job.models import JobRecord, TaskSpec
from secpt.core.job.resume import jobs_dir, make_run_config, read_task_snapshot, write_task_snapshot
from secpt.core.job.rounds import load_latest_round
from secpt.core.job.store import JobStore
from secpt.core.profile.render import DEFAULT_CONSTRAINTS, load_prior, render_system_extra
from secpt.runtime import engine as _engine


def _api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    return key


def run_engine(
    *,
    objective: str,
    profile: str = "ctf",
    prior: str | None = None,
    job_id: str | None = None,
    workspace: str | None = None,
    max_steps: int | None = None,
    max_wall_s: int | None = None,
    model_primary: str = "deepseek-chat",
    model_fallback: str = "deepseek-reasoner",
    job_store: JobStore | None = None,
    llm=None,
    stage=None,
    verbose: bool = False,
) -> _engine.RunResult:
    """新建任务跑一次 Engine，全程落盘。profile 决定 tools/terminator/budget/先验。

    stage（多阶段编排用）：Stage 对象，覆盖 role/tools/prior/stop_condition。
    verbose：引擎步进详情输出。
    """

    job_id = job_id or uuid.uuid4().hex[:8]

    if llm is None:
        if os.environ.get("SECPT_FAKE_LLM"):
            # 与 worker 一致：SECPT_FAKE_LLM=1 时用假 LLM（无 key 可测）
            from secpt.core.job.fakellm import FakeLLM

            llm = FakeLLM(n_steps=max(1, (max_steps or 100) - 1), job_id=job_id)
        else:
            _api_key()  # 真实 LLM 路径才需要 key；fake 路径无 key 可跑
            from secpt.runtime.llm.client import call_llm

            llm = call_llm

    jdir = jobs_dir() / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    if workspace is None:
        workspace = str(jdir)

    # S4：profile 数据驱动（role/tools/terminator/budget/constraints）
    from secpt.core.profile.loader import load_profile

    prof = load_profile(profile)

    # 阶段覆盖（多阶段编排）：role/tools/prior/stop_condition
    stage_role = getattr(stage, "role", "") or prof.role
    stage_tools = list(getattr(stage, "tools", None) or prof.tools)
    stage_prior = getattr(stage, "prior", "") or ""
    stage_stop = getattr(stage, "stop_condition", "") or ""

    prior_name = prior or (stage_prior or None)
    # --prior 覆盖：显式指定先验文件（如 saml），必须存在；否则用 profile 先验
    prior_text = load_prior(prior_name) if prior_name is not None else prof.prior_text()

    # 插件工具装配：tools 列表里已安装插件名 → extra_tools；其余按原语义并入 bash 白名单
    from secpt.tools.plugin_loader import discover_plugins, engine_tools_for

    _yp, _pt = discover_plugins()
    plugin_names = set(_yp) | set(_pt)
    extra_tools = engine_tools_for([t for t in stage_tools if t in plugin_names])
    # report_finding 走结构化工具通道，不进 bash 白名单
    bash_extra = [t for t in stage_tools if t not in plugin_names and t != "report_finding"]

    # report_finding：profile/stage 声明即装配的结构化发现上报工具（验证纪律入口）
    from secpt.findings.tool import maybe_attach

    extra_tools, _step_box = maybe_attach(extra_tools, stage_tools, job_id)
    bash_extra = [t for t in bash_extra if t != "report_finding"]

    store = job_store or JobStore()

    # 构造 RunConfig + system
    cfg = _engine.RunConfig(
        job_id=job_id,
        objective=objective,
        system_extra="",
        workspace=workspace,
        model_primary=model_primary,
        model_fallback=model_fallback,
        flag_regex=prof.terminator.flag_regex,
        max_steps=prof.budget.steps if max_steps is None else max_steps,
        max_wall_s=prof.budget.wall_s if max_wall_s is None else max_wall_s,
    )
    cfg.bash_whitelist = (
        frozenset(_engine.DEFAULT_BASH_WHITELIST)
        | frozenset(prof.merged_whitelist_extra())
        | frozenset(t for t in bash_extra if t)
    )
    # 显式窗口覆盖（如网关把 deepseek-chat 映射到 128k 上下文的 flash）：
    # SECPT_CONTEXT_WINDOW=128000；未设置则引擎按 MODEL_WINDOWS 查表
    _ctx_env = os.environ.get("SECPT_CONTEXT_WINDOW")
    if _ctx_env:
        cfg.context_window = int(_ctx_env)
    constraints = list(prof.constraints) or [DEFAULT_CONSTRAINTS]
    stop_cond = stage_stop or prof.terminator.stop_condition
    if stop_cond:
        constraints.append(stop_cond)
    _tools_doc = _engine.build_tools_doc(cfg)
    if "report_finding" in extra_tools:
        from secpt.findings.tool import TOOL_DOC as _RF_DOC

        _tools_doc += "\n" + _RF_DOC
    cfg.system_extra = render_system_extra(
        profile,
        objective=objective,
        tools_doc=_tools_doc,
        prior=prior_text,
        role=stage_role,
        constraints="\n".join(constraints),
    )

    # 历史经验注入（learning 闭环；SECPT_EXPERIENCE=0 关闭）
    try:
        from secpt.learning.retrieve import experience_section

        exp_sec = experience_section(objective)
        if exp_sec:
            cfg.system_extra += "\n\n" + exp_sec
    except Exception as e:
        print(f"[learn] experience injection failed (ignored): {e}")

    # TaskSpec 快照（含 run_config，resume 时重建）
    from secpt.core.job.models import TaskBudget

    spec = TaskSpec(
        job_id=job_id,
        task_type=profile,
        objective=objective,
        budget=TaskBudget(wall_s=cfg.max_wall_s, steps=cfg.max_steps),
    )
    run_cfg_dict = {
        "workspace": workspace,
        "max_steps": cfg.max_steps,
        "max_wall_s": cfg.max_wall_s,
        "bash_timeout_s": cfg.bash_timeout_s,
        "bash_whitelist": sorted(cfg.bash_whitelist),
        "flag_regex": cfg.flag_regex,
        "model_primary": cfg.model_primary,
        "model_fallback": cfg.model_fallback,
        "stuck_threshold": cfg.stuck_threshold,
        "stuck_increment": cfg.stuck_increment,
        "context_window": cfg.context_window,
        "budget_cost_cny": cfg.budget_cost_cny,
        "round_max_steps": cfg.round_max_steps,
        "round_max_wall_s": cfg.round_max_wall_s,
        "extra_tool_names": sorted(extra_tools),
    }
    write_task_snapshot(jdir, spec, run_cfg_dict)

    # 事件流 hooks（S3：外包 watchdog 空转检测）
    sink = EventSink(job_id, jobs_dir(), store=store)
    from secpt.core.watchdog import Watchdog

    hooks = Watchdog(sink, store=store, job_id=job_id)
    if "report_finding" in extra_tools:
        from secpt.findings.tool import StepTracker

        hooks = StepTracker(hooks, _step_box)

    # JobRecord
    rec = JobRecord(job_id=job_id, spec=spec, status="running")
    store.upsert_job(rec)
    sink.status_change("running")

    try:
        result = asyncio.run(
            _engine.Engine(
                cfg, llm=llm, hooks=hooks, extra_tools=extra_tools or None, verbose=verbose
            ).run()
        )
    except Exception as e:
        result = _engine.RunResult(flag=None, steps=0, wall_s=0.0, status="error", messages=[])
        rec = store.get_job(job_id) or rec
        rec.set_status("failed", error=str(e))
        store.upsert_job(rec)
        sink.error(str(e))
        raise

    rec = store.get_job(job_id) or rec
    rec.steps = result.steps
    if result.flag:
        rec.set_status("succeeded")
        sink.finding("flag", "flag", result.flag, "engine result")
    else:
        _apply_terminal_status(rec, result)
    store.upsert_job(rec)
    sink.status_change(rec.status)

    result.job_id = job_id  # type: ignore[attr-defined]
    return result


def resume_engine(
    job_id: str,
    *,
    job_store: JobStore | None = None,
    llm=None,
) -> _engine.RunResult:
    """从磁盘断点继续任务。"""
    jdir = jobs_dir() / job_id
    if not (jdir / "task.yaml").is_file():
        raise FileNotFoundError(f"job {job_id} 不存在: {jdir}")
    spec, run_cfg = read_task_snapshot(jdir)

    if llm is None:
        if os.environ.get("SECPT_FAKE_LLM"):
            # 与 worker 一致：SECPT_FAKE_LLM=1 时用假 LLM（无 key 可测）
            from secpt.core.job.fakellm import FakeLLM

            max_steps = run_cfg.get("max_steps", spec.budget.steps)
            llm = FakeLLM(n_steps=max(1, max_steps - 1), job_id=job_id)
        else:
            _api_key()  # 真实 LLM 路径才需要 key；fake 路径无 key 可跑
            from secpt.runtime.llm.client import call_llm

            llm = call_llm

    store = job_store or JobStore()
    n, messages = load_latest_round(job_id, jobs_dir())

    cfg = make_run_config(job_id, spec, run_cfg)
    if messages:
        # 初始链含首条 system，直接复用（保 prefix cache）
        cfg.initial_messages = messages
        cfg.round_offset = n
    else:
        # 无存档 → 从头开始（重建 system，profile 数据驱动）
        from secpt.core.profile.loader import load_profile
        from secpt.core.profile.render import render_system_extra as _rse

        _prof = load_profile(spec.task_type)
        _cons = list(_prof.constraints) or [DEFAULT_CONSTRAINTS]
        if _prof.terminator.stop_condition:
            _cons.append(_prof.terminator.stop_condition)
        cfg.system_extra = _rse(
            spec.task_type,
            objective=spec.objective,
            tools_doc=_engine.build_tools_doc(cfg),
            prior=_prof.prior_text(),
            role=_prof.role,
            constraints="\n".join(_cons),
        )

    sink = EventSink(job_id, jobs_dir(), store=store)
    from secpt.core.watchdog import Watchdog

    hooks = Watchdog(sink, store=store, job_id=job_id)
    rec = store.get_job(job_id) or JobRecord(job_id=job_id, spec=spec, status="running")
    rec.set_status("running")
    rec.round = n
    store.upsert_job(rec)
    sink.status_change("running", round_=n)

    # 快照里的插件工具重新装配（插件定义可能已更新）
    from secpt.tools.plugin_loader import engine_tools_for

    extra_tools = engine_tools_for(run_cfg.get("extra_tool_names") or [])
    from secpt.findings.tool import StepTracker, maybe_attach

    extra_tools, _step_box = maybe_attach(
        extra_tools, run_cfg.get("extra_tool_names") or [], job_id
    )

    if "report_finding" in extra_tools:
        hooks = StepTracker(hooks, _step_box)
    result = asyncio.run(
        _engine.Engine(cfg, llm=llm, hooks=hooks, extra_tools=extra_tools or None).run()
    )

    rec = store.get_job(job_id) or rec
    rec.steps = result.steps
    if result.flag:
        rec.set_status("succeeded")
        sink.finding("flag", "flag", result.flag, "engine result")
    else:
        _apply_terminal_status(rec, result)
    store.upsert_job(rec)
    sink.status_change(rec.status)

    result.job_id = job_id  # type: ignore[attr-defined]
    return result


def _apply_terminal_status(rec: JobRecord, result: _engine.RunResult) -> None:
    """RunResult.status → JobRecord.status（精确语义，超步数≠超预算）。"""
    if result.status == "error":
        rec.set_status("failed", error="engine error")
    elif result.status in ("budget_exceeded", "step_exceeded", "wallclock_exceeded"):
        rec.set_status(result.status)
    else:
        rec.set_status("failed")
