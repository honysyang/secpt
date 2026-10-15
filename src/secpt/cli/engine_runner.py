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
import contextlib
import os
import shutil
import sys
import uuid
from pathlib import Path

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


def resolve_models(primary: str | None, fallback: str | None) -> tuple[str, str]:
    """模型名解析：显式参数 > 环境变量 > 内置默认。

    SECPT_MODEL_PRIMARY / SECPT_MODEL_FALLBACK 让模型选择在 .env 里统一配置，
    与 SECPT_MODEL_BASE_/KEY_/POOL 生态一致（provider/model 形式同样可用）。
    """
    primary = primary or os.environ.get("SECPT_MODEL_PRIMARY") or "deepseek-chat"
    fallback = fallback or os.environ.get("SECPT_MODEL_FALLBACK") or "deepseek-reasoner"
    return primary, fallback


def _prod_llm():
    """生产 LLM 入口：SECPT_MODEL_POOL 设置时走多 provider 故障转移链。"""
    if os.environ.get("SECPT_FAKE_LLM"):
        # fake 环境（无 key 可测）：与 run_engine 的 worker 分支一致，统一 FakeLLM——
        # 否则 run_frontier/planner 解析到真实客户端会 401 空烧（solve --mode frontier 冒烟即此）
        from secpt.core.job.fakellm import FakeLLM

        return FakeLLM(n_steps=1)
    if os.environ.get("SECPT_MODEL_POOL"):
        from secpt.runtime.llm.pool import call_pooled

        return call_pooled
    from secpt.runtime.llm.client import call_llm

    return call_llm


def write_attachments_index(work_dir: Path, artifacts: list[str]) -> None:
    """把附件落到工作区，保证容器内也能读到（P1 断链修复）。

    断链根因：此前只建指向宿主机绝对路径的 symlink，而沙箱容器只挂载工作区，
    容器内访问 symlink 目标必然失效（run-0939 已见 No such file）。现在把附件
    （文件/目录）以真实副本落到 ws/attachments/ 下，容器内用相对短路径
    attachments/xxx 直接可读；ATTACHMENTS.md 仍列绝对路径供宿主侧参考。
    """
    if not artifacts:
        return
    try:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        adir = work_dir / "attachments"
        adir.mkdir(parents=True, exist_ok=True)
        lines = ["# 附件源码索引（attachments/ 下已有真实副本，直接用相对路径读取）", ""]
        for a in artifacts:
            ap = Path(a)
            if not ap.is_absolute():
                ap = ap.resolve()
            if not ap.exists():
                lines.append(f"- {ap}（宿主侧缺失，已跳过）")
                continue
            dest = adir / ap.name
            if ap.is_dir():
                # 目录副本（覆盖式重建，防旧副本残留）
                with contextlib.suppress(OSError):
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(ap, dest)
            else:
                with contextlib.suppress(OSError):
                    if dest.exists():
                        dest.unlink()
                    shutil.copy2(ap, dest)
            lines.append(f"- {ap} → attachments/{ap.name}")
        (work_dir / "ATTACHMENTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"[engine] 附件索引写入失败（忽略）: {e}")


def run_engine(
    *,
    objective: str,
    profile: str = "ctf",
    prior: str | None = None,
    job_id: str | None = None,
    workspace: str | None = None,
    artifacts: list[str] | None = None,
    max_steps: int | None = None,
    max_wall_s: int | None = None,
    model_primary: str | None = None,
    model_fallback: str | None = None,
    job_store: JobStore | None = None,
    llm=None,
    stage=None,
    verbose: bool = False,
    sandbox: str = "none",
    hook_factory=None,  # 外部包装层工厂：inner_hooks -> hooks（团队模式完成广播让出用）
    scope_entries: list[str] | None = None,  # G1 目标授权白名单来源（题面入口/意图资产）
) -> _engine.RunResult:
    """新建任务跑一次 Engine，全程落盘。profile 决定 tools/terminator/budget/先验。

    stage（多阶段编排用）：Stage 对象，覆盖 role/tools/prior/stop_condition。
    hook_factory：在内部 hooks 链（watchdog/scribe/steer）最外层再包一层，
      用于注入协作感知（如 per-asset 完成广播让出）；None 表示不包（向后兼容）。
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
            llm = _prod_llm()

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
    # 内置四工具（CORE_TOOLS）与 report_finding 走结构化通道，绝不进 bash 白名单——
    # profile tools 里的 "bash" 是内置工具名，误并入白名单会放开 bash -lc _shell 解释
    from secpt.core.profile.loader import CORE_TOOLS

    bash_extra = [
        t
        for t in stage_tools
        if t not in plugin_names and t != "report_finding" and t not in CORE_TOOLS
    ]

    # report_finding：profile/stage 声明即装配的结构化发现上报工具（验证纪律入口）
    from secpt.findings.tool import maybe_attach

    extra_tools, _step_box = maybe_attach(extra_tools, stage_tools, job_id)
    from secpt.intel.tools import maybe_attach_intel

    extra_tools = maybe_attach_intel(extra_tools, stage_tools, job_id, box=_step_box)
    bash_extra = [t for t in bash_extra if t != "report_finding"]

    store = job_store or JobStore()

    # 构造 RunConfig + system
    model_primary, model_fallback = resolve_models(model_primary, model_fallback)
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
    # profile 级 bash 工具超时覆盖（pwn 等长交互场景）
    if getattr(prof, "bash_timeout_s", 0) > 0:
        cfg.bash_timeout_s = prof.bash_timeout_s
    if getattr(prof, "stuck_wait_cap", 0) > 0:
        cfg.stuck_wait_cap = prof.stuck_wait_cap
    _wait_env = os.environ.get("SECPT_STUCK_WAIT_CAP")
    if _wait_env:
        with contextlib.suppress(ValueError):
            cfg.stuck_wait_cap = max(1, int(_wait_env))
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

    # G1+G5 治理装配：目标授权范围 + per-job JSONL 审计（治理与生命周期事件）
    from secpt.governance.audit import AuditLogger
    from secpt.governance.scope import build_scope_validator

    cfg.scope = build_scope_validator(scope_entries)
    cfg.audit = AuditLogger(
        str(Path(os.environ.get("SECPT_VAR", "var")) / "audit" / f"{job_id}.jsonl")
    )
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
        from secpt.learning.retrieve import experience_section, search_experiences

        exps = search_experiences(objective, task_type=profile)
        if exps:
            cfg.system_extra += "\n\n" + experience_section(objective, task_type=profile)
            print(
                f"[learn] 注入 {len(exps)} 条历史经验（objective 检索命中）："
                f"{'；'.join(e.title[:24] for e in exps[:3])}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[learn] experience injection failed (ignored): {e}")

    # TaskSpec 快照（含 run_config，resume 时重建）
    from secpt.core.job.models import TaskBudget

    spec = TaskSpec(
        job_id=job_id,
        task_type=profile,
        objective=objective,
        budget=TaskBudget(wall_s=cfg.max_wall_s, steps=cfg.max_steps),
        entries=list(scope_entries or []),  # G1：scope 白名单来源随快照持久化（resume 一致）
    )
    cfg.sandbox = getattr(prof, "sandbox", "") or sandbox
    # profile 指定沙箱镜像（pwn/crypto 等重工具镜像）：运行期注入 env，结束恢复
    _img_env = None
    if getattr(prof, "sandbox_image", ""):
        _img_env = os.environ.get("SECPT_SANDBOX_IMAGE")
        os.environ["SECPT_SANDBOX_IMAGE"] = prof.sandbox_image
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
        "stuck_wait_cap": cfg.stuck_wait_cap,
        "context_window": cfg.context_window,
        "budget_cost_cny": cfg.budget_cost_cny,
        "round_max_steps": cfg.round_max_steps,
        "round_max_wall_s": cfg.round_max_wall_s,
        "extra_tool_names": sorted(extra_tools),
        "sandbox": cfg.sandbox,
    }
    write_task_snapshot(jdir, spec, run_cfg_dict)

    # 事件流 hooks（S3：外包 watchdog 空转检测）
    sink = EventSink(job_id, jobs_dir(), store=store)
    from secpt.core.watchdog import Watchdog

    hooks = Watchdog(sink, store=store, job_id=job_id)
    if "report_finding" in extra_tools:
        from secpt.findings.tool import StepTracker

        hooks = StepTracker(hooks, _step_box)
    from secpt.intel.scribe import GraphScribe

    hooks = GraphScribe(hooks, job_id=job_id)
    from secpt.core.orchestrator.steer import SteerHook

    hooks = SteerHook(hooks, job_id=job_id)

    if hook_factory is not None:
        # 外部包装层（团队模式 per-asset 完成广播让出）：包在最外层，
        # on_step 返回 steer 消息由 engine 自动注入 user 消息引导收尾
        hooks = hook_factory(hooks)

    # JobRecord
    write_attachments_index(cfg.work_dir, artifacts or [])

    rec = JobRecord(job_id=job_id, spec=spec, status="running")
    store.upsert_job(rec)
    sink.status_change("running")
    with contextlib.suppress(Exception):
        cfg.audit.log("job.started", profile=profile, objective=(objective or "")[:120])

    from secpt.traffic.harness import traffic_scope

    try:
        with traffic_scope(job_id, enabled=prof.traffic):
            result = asyncio.run(
                _engine.Engine(
                    cfg,
                    llm=llm,
                    hooks=hooks,
                    extra_tools=extra_tools or None,
                    verbose=verbose,
                ).run()
            )
    except Exception as e:
        result = _engine.RunResult(flag=None, steps=0, wall_s=0.0, status="error", messages=[])
        rec = store.get_job(job_id) or rec
        rec.set_status("failed", error=str(e))
        store.upsert_job(rec)
        sink.error(str(e))
        with contextlib.suppress(Exception):
            cfg.audit.log("job.finished", status="error", steps=0, wall_s=0.0)
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
    with contextlib.suppress(Exception):
        cfg.audit.log(
            "job.finished", status=result.status, steps=result.steps, wall_s=result.wall_s
        )

    if _img_env is None:
        os.environ.pop("SECPT_SANDBOX_IMAGE", None)
    else:
        os.environ["SECPT_SANDBOX_IMAGE"] = _img_env

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
            llm = _prod_llm()

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
    from secpt.intel.tools import maybe_attach_intel

    extra_tools = maybe_attach_intel(
        extra_tools, run_cfg.get("extra_tool_names") or [], job_id, box=_step_box
    )

    if "report_finding" in extra_tools:
        hooks = StepTracker(hooks, _step_box)
    from secpt.intel.scribe import GraphScribe

    hooks = GraphScribe(hooks, job_id=job_id)
    from secpt.core.orchestrator.steer import SteerHook

    hooks = SteerHook(hooks, job_id=job_id)
    from secpt.core.profile.loader import load_profile as _load_profile
    from secpt.traffic.harness import traffic_scope

    _prof_traffic = _load_profile(spec.task_type).traffic
    with traffic_scope(job_id, enabled=_prof_traffic):
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
