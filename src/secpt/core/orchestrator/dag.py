"""S5 最小 DAG：recon → [exploit × N 并行] → verify。

分流规则（红线 2 / 对照实验结论）：
- 默认（未显式 use_dag）→ 单链：一次 Engine.run，入口拼进 objective，失败自动重试一次
- 显式 use_dag=True（--mode dag）且 N >= 2 → frontier 角色意图编排（recon→exploit×N→verify→report）
- N == 1（或未声明多入口）→ 编排层天然休眠，直连单链

DAG 编排层仍保留（未证伪、未被证实），但默认关闭；只有显式 --mode dag 才启用。
N = 入口数量（spec.entries）。固定 DAG 编排器（SpecialistOrchestrator）已由
frontier 角色意图折叠退役（见 policy.DEPRECATED_MODULES），WarroomHooks 迁至
core/memory/warroom.py。
"""

from __future__ import annotations

import asyncio

from secpt.core.job.models import TaskSpec
from secpt.core.job.store import JobStore

from .policy import SINGLE_ENTRY_MAX
from .summary import ChainResult, RunSummary


def entries_of(spec: TaskSpec) -> list[str]:
    """任务入口列表；entries 为空时按单个隐式入口处理。"""
    return list(spec.entries or [])


def needs_orchestrator(spec: TaskSpec, *, use_dag: bool = False) -> bool:
    """是否走 DAG：显式启用(use_dag) 且 多入口。默认关闭（对照组结论：DAG 降级为可选）。"""
    return use_dag and len(entries_of(spec)) > SINGLE_ENTRY_MAX


async def run_task(
    spec: TaskSpec,
    *,
    store: JobStore | None = None,
    llm=None,
    use_orchestrator: bool = True,
    db_path: str = "var/secpt.sqlite",
    model_primary: str = "deepseek-chat",
    model_fallback: str = "deepseek-reasoner",
) -> dict:
    """按 spec 分流。默认单链（失败自动重试一次）；显式 use_dag 才走 DAG。

    use_orchestrator 参数语义保留（默认 True 仅维持既有签名），
    但在 CLI 层默认 false；真正决定是否启编排的是
    needs_orchestrator(spec, use_dag=use_orchestrator)。
    返回汇总 dict（兼容两分支字段），chains 含 attempt-1/attempt-2。
    """
    # 目标拆解层（阶段一）：spec.objective 未经结构化且非 benchmark 时先拆解再分流
    from secpt.core.orchestrator.goals import (
        decompose_objective,
        is_rendered,
        render_goal_system,
        seed_goal_assets,
    )

    if spec.task_type != "benchmark" and not is_rendered(spec.objective):
        goal = await decompose_objective(spec.objective, llm=llm, model=model_primary)
        seed_goal_assets(goal)
        spec.objective = render_goal_system(goal)

    use_dag = needs_orchestrator(spec, use_dag=use_orchestrator)
    if not use_dag:
        return await run_single_chain(
            spec,
            store=store,
            llm=llm,
            db_path=db_path,
            model_primary=model_primary,
            model_fallback=model_fallback,
        )

    # P2 执行策略路由（跑关+渗透统一编排）：benchmark → 并行跑分器；
    # 多入口渗透 → frontier 角色意图编排（recon→exploit×N 并行→verify→report）
    from .policy import route_policy

    policy = route_policy(spec.task_type, entries_of(spec))
    if policy == "benchmark":
        from .benchmark import run_benchmark_campaign

        # A2：dag 入口同样走战役多轮（否则只有 solve 路径享受自动重试）
        summary = await run_benchmark_campaign(spec, llm=llm, model=model_primary)
        summary["entries"] = list(entries_of(spec))  # 入口维度来自 TaskSpec（编排器不感知）
        return summary

    from .frontier import run_frontier

    summary = await run_frontier(
        spec.objective,
        profile=spec.task_type,
        base_job_id=spec.job_id,
        llm=llm,
        planner_llm=llm,  # 团队模式规划与执行同源（测试/低成本路径不碰真实 API）
        model=model_primary,
        role_plan=True,
        default_assets=list(entries_of(spec)),
    )
    summary["mode"] = "team"  # 契约 dict 可变：团队语义覆盖 frontier 默认 mode
    summary["entries"] = list(entries_of(spec))  # 入口维度来自 TaskSpec（frontier 不感知）
    return summary


def augment_objective(objective: str, entries: list[str], artifacts: list[str]) -> str:
    """objective 拼接【目标入口】【附件源码】块。

    solve 各执行模式与 dag 单链共用：solve 直调 run_frontier 的路径（团队模式/
    懒升级）不经 dag.run_single_chain，不拼这两块会丢附件绝对路径——engine 的
    bash 工作目录在 var/jobs/<id>/ws/ 下，相对路径会漂移（author_plus 事故口径）。
    """
    if entries:
        objective += "\n\n【目标入口】\n" + "\n".join(f"- {e}" for e in entries)
    if artifacts:
        objective += "\n\n【附件源码（已就位，用 read_file 读取）】\n" + "\n".join(
            f"- {a}" for a in artifacts
        )
    return objective


async def run_single_chain(
    spec: TaskSpec,
    *,
    store: JobStore | None = None,
    llm=None,
    db_path: str = "var/secpt.sqlite",
    model_primary: str = "deepseek-chat",
    model_fallback: str = "deepseek-reasoner",
    _attempt: int = 1,
) -> dict:
    """单链（默认路径）：一次 Engine.run，入口拼进 objective；失败换路线重试一次。

    重试使用独立 job_id（<原id>-r2）与新 workspace，绝不复用第一次的 workspace。
    chains 汇总带 attempt-1 / attempt-2，便于事后判定重试是否必要。
    """
    from secpt.cli.engine_runner import run_engine

    entries = entries_of(spec)
    chains: list[dict] = []
    flags: list[str] = []
    wall_s_total = 0.0

    objective = augment_objective(spec.objective, entries, list(spec.artifacts or []))

    # spec.budget 语义：0=未指定（用 profile）；>0=该值；<0=不限（7×24，--wall 0 显式不限）
    max_steps = (
        spec.budget.steps if spec.budget.steps > 0 else (0 if spec.budget.steps < 0 else None)
    )
    max_wall_s = (
        spec.budget.wall_s if spec.budget.wall_s > 0 else (0 if spec.budget.wall_s < 0 else None)
    )

    # attempt-1：主尝试
    result = await asyncio.to_thread(
        run_engine,
        objective=objective,
        profile=spec.task_type,
        prior=spec.prior,
        job_id=spec.job_id,
        job_store=store,
        llm=llm,
        max_steps=max_steps,
        max_wall_s=max_wall_s,
        model_primary=model_primary,
        model_fallback=model_fallback,
        artifacts=spec.artifacts,
    )
    chains.append(
        ChainResult(
            role="exploit",
            entry=-1,  # 单链无入口维度（entries 已并入 objective）
            flag=result.flag,
            steps=result.steps,
            wall_s=round(result.wall_s, 1),
            status=result.status,
        )
    )
    wall_s_total += result.wall_s
    if result.flag:
        flags.append(result.flag)

    # attempt-2：仅当第一轮未拿到 flag 且资源耗尽/失败时重试（新 job_id 防污染）。
    # stalled 走 RESET 重建（设计 §3：换会话从外部状态重建，而非同会话续步）；
    # 其余终态走原 r2 续步（带上一轮成果摘要）。
    if not result.flag and result.status in (
        "budget_exceeded",
        "step_exceeded",
        "wallclock_exceeded",
        "stalled",  # 止损停滞同属"未拿到 flag 的可重试终态"
        "failed",
    ):
        import os as _os

        _direct_reset_on = _os.environ.get("SECPT_DIRECT_RESET", "1") != "0"
        if result.status == "stalled" and _direct_reset_on:
            from secpt.core.orchestrator.frontier import (
                _carry_ws_artifacts,
                _has_ws_output,
                _reset_handoff_summary,
            )

            if _has_ws_output(spec.job_id):
                # RESET 门 = 首尝才重建（单链 attempt-2 即首次重试，天然满足）+
                # ws 有实质产出（空转不重建，走下方 exhausted 语义）
                reset_job_id = f"{spec.job_id}-rs"
                manifest = _carry_ws_artifacts(spec.job_id, reset_job_id)
                handoff = await _reset_handoff_summary(result, objective, llm)
                reset_obj = (
                    objective + "\n\n【RESET 重建（编排层注入）】上一会话已策略级卡死（stalled），"
                    "本会话从外部状态重建上下文，严禁原样重跑旧路线：\n"
                    f"1. 上轮 handoff 摘要：\n{handoff}\n"
                    f"2. 已复制工作区产物（继续用，不要推倒重来）：\n{manifest}\n"
                    "基于以上状态选一条与上轮不同的路线推进，尽快以 ANSWER: 收尾。"
                )
                result2 = await asyncio.to_thread(
                    run_engine,
                    objective=reset_obj,
                    profile=spec.task_type,
                    prior=spec.prior,
                    job_id=reset_job_id,
                    job_store=store,
                    llm=llm,
                    max_steps=max_steps,
                    max_wall_s=max_wall_s,
                    model_primary=model_primary,
                    model_fallback=model_fallback,
                )
                try:
                    from secpt.runtime.metrics import counter_inc

                    counter_inc(
                        "loop_reset_total",
                        outcome=f"direct_{'recovered' if result2.flag else 'exhausted'}",
                    )
                except Exception:
                    pass
                import contextlib

                with contextlib.suppress(Exception):
                    from secpt.runtime.notify import notify

                    notify(
                        "job_reset",
                        f"secpt 单链 RESET 重建 {reset_job_id}",
                        f"上轮 stalled 换会话重建，结果：{'拿到成果' if result2.flag else '未收尾'}",
                    )
                chains.append(
                    ChainResult(
                        role="exploit",
                        entry=-1,
                        flag=result2.flag,
                        steps=result2.steps,
                        wall_s=round(result2.wall_s, 1),
                        status=result2.status,
                    )
                )
                wall_s_total += result2.wall_s
                if result2.flag:
                    flags.append(result2.flag)
                return RunSummary(
                    job_id=spec.job_id,
                    mode="single(+retry)",
                    entries=entries,
                    flags=flags,
                    chains=chains,
                    wall_s=round(wall_s_total, 1),
                    extras={"recon_assets": 0, "reset": True},  # RESET 重建标记（对照判据）
                ).to_dict()

        # 上一轮成果摘要：循环型长跑任务（benchmark/评测）里，"重试"是续跑——
        # 已解出的答案必须带过去，否则 r2 会重打已通的题白烧步数
        prev_answers: list[str] = []
        for m in reversed(result.messages):
            content = str(m.get("content") or "")
            if m.get("role") == "assistant" and "ANSWER:" in content:
                prev_answers.append(content.strip()[:1500])
                if len(prev_answers) >= 2:
                    break
        prev_txt = "\n".join(reversed(prev_answers))
        retry_note = (
            "\n\n【上一次尝试未成功】\n"
            f"上次用了 {result.steps} 步，最终状态 {result.status}。\n"
            "请换一条不同的技术路线（例如：上次走协议攻击就改走凭据/配置；"
            "上次黑盒就先找源码；上次卡在工具用法上就换更简单的命令）。\n"
            "不要把上次的步骤原样重跑一遍。"
            + (
                f"\n\n【上一轮已产出的成果（直接继承，不要重复劳动）】\n{prev_txt}"
                if prev_txt
                else ""
            )
        )
        retry_job_id = f"{spec.job_id}-r2"
        result2 = await asyncio.to_thread(
            run_engine,
            objective=objective + retry_note,
            profile=spec.task_type,
            prior=spec.prior,
            job_id=retry_job_id,
            job_store=store,
            llm=llm,
            max_steps=max_steps,
            max_wall_s=max_wall_s,
            model_primary=model_primary,
            model_fallback=model_fallback,
        )
        chains.append(
            ChainResult(
                role="exploit",
                entry=-1,
                flag=result2.flag,
                steps=result2.steps,
                wall_s=round(result2.wall_s, 1),
                status=result2.status,
            )
        )
        wall_s_total += result2.wall_s
        if result2.flag:
            flags.append(result2.flag)

    return RunSummary(
        job_id=spec.job_id,
        # 单链路径恒标 single(+retry)：重试能力始终装配（首轮成功则 chains 仅 1 条，
        # 首轮失败自动换路线重试则 chains 2 条 attempt-1/attempt-2），便于对照判据。
        mode="single(+retry)",
        entries=entries,
        flags=flags,
        chains=chains,
        wall_s=round(wall_s_total, 1),
        extras={"recon_assets": 0},  # 历史键（recon 属 frontier 口径），保留兼容
    ).to_dict()
