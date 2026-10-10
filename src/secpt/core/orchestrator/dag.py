"""S5 最小 DAG：recon → [exploit × N 并行] → verify。

分流规则（红线 2 / 对照实验结论）：
- 默认（未显式 use_dag）→ 单链：一次 Engine.run，入口拼进 objective，失败自动重试一次
- 显式 use_dag=True（--mode dag）且 N >= 2 → SpecialistOrchestrator 跑 DAG
- N == 1（或未声明多入口）→ 编排层天然休眠，直连单链

DAG 编排层仍保留（未证伪、未被证实），但默认关闭；只有显式 --mode dag 才启用。
N = 入口数量（spec.entries）。
"""

from __future__ import annotations

import asyncio

from secpt.core.job.models import TaskSpec
from secpt.core.job.store import JobStore

from .policy import SINGLE_ENTRY_MAX


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

    from .specialist import SpecialistOrchestrator

    orch = SpecialistOrchestrator(spec, store=store, llm=llm, db_path=db_path)
    summary = await orch.run()
    summary["mode"] = "dag"
    return summary


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

    objective = spec.objective
    if entries:
        objective += "\n\n【目标入口】\n" + "\n".join(f"- {e}" for e in entries)
    if spec.artifacts:
        objective += "\n\n【附件源码（已就位，用 read_file 读取）】\n" + "\n".join(
            f"- {a}" for a in spec.artifacts
        )

    # spec.budget 权威：0=未指定用 profile，<0=不限（7×24）
    max_steps = spec.budget.steps if spec.budget.steps != 0 else None
    max_wall_s = spec.budget.wall_s if spec.budget.wall_s != 0 else None
    if spec.budget.steps < 0:
        max_steps = 0
    if spec.budget.wall_s < 0:
        max_wall_s = 0

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
        {
            "role": "exploit",
            "entry": -1,
            "flag": result.flag,
            "steps": result.steps,
            "wall_s": round(result.wall_s, 1),
            "status": result.status,
        }
    )
    wall_s_total += result.wall_s
    if result.flag:
        flags.append(result.flag)

    # attempt-2：仅当第一轮未拿到 flag 且资源耗尽/失败时重试（新 job_id 防污染）
    if not result.flag and result.status in (
        "budget_exceeded",
        "step_exceeded",
        "wallclock_exceeded",
        "failed",
    ):
        retry_note = (
            "\n\n【上一次尝试未成功】\n"
            f"上次用了 {result.steps} 步，最终状态 {result.status}。\n"
            "请换一条不同的技术路线（例如：上次走协议攻击就改走凭据/配置；"
            "上次黑盒就先找源码；上次卡在工具用法上就换更简单的命令）。\n"
            "不要把上次的步骤原样重跑一遍。"
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
            {
                "role": "exploit",
                "entry": -1,
                "flag": result2.flag,
                "steps": result2.steps,
                "wall_s": round(result2.wall_s, 1),
                "status": result2.status,
            }
        )
        wall_s_total += result2.wall_s
        if result2.flag:
            flags.append(result2.flag)

    return {
        "job_id": spec.job_id,
        # 单链路径恒标 single(+retry)：重试能力始终装配（首轮成功则 chains 仅 1 条，
        # 首轮失败自动换路线重试则 chains 2 条 attempt-1/attempt-2），便于对照判据。
        "mode": "single(+retry)",
        "entries": entries,
        "recon_assets": 0,
        "warroom_assets": 0,
        "warroom_attempts": 0,
        "findings": 0,
        "verified": 0,
        "flags": flags,
        "chains": chains,
        "wall_s": round(wall_s_total, 1),
    }
