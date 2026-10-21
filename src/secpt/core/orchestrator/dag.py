"""统一执行路由（S6 模式收敛）：benchmark → campaign；其余 → frontier。

2026-10-19 模式收敛——单一执行编排：frontier 意图驱动。原三种执行模式
（single 单链 / stages 多阶段 / frontier 团队）统一收敛：

- 原 single 单链   → frontier（planner 驱动 · stop_on_first_flag=True ·
  profile.workers 并发；失败恢复由意图复核 / RESET / 升级强模型承担，
  优于旧 attempt-2 固定重试一次）
- 原 stages 多阶段 → profile.stages 折叠为线性 depends_on 意图链
  （run_frontier stages= 参数；交接复用 M1 同资产 asset_state 搬运）
- 多资产          → 通用执行体并行认领独立任务（v3：不再使用角色模板骨架；
  planner 产独立任务，main/logistics 两通道由 triage 快判分流）
- benchmark        → campaign 保留（平台协议层：拉题/结算/账本防双开，
  内部即 run_frontier；不是 --mode 语义的"模式"）

已退役（policy.DEPRECATED_MODULES 硬约束，不得复活）：
run_single_chain / needs_orchestrator / entries_of / stages.run_stages。
"""

from __future__ import annotations

import os
import time

from secpt.core.job.models import TaskSpec
from secpt.core.job.store import JobStore


def augment_objective(objective: str, entries: list[str], artifacts: list[str]) -> str:
    """objective 拼接【目标入口】【附件源码】块。

    solve 直调 run_frontier 的路径不经 run_task 时必须显式拼这两块——engine 的
    bash 工作目录在 var/jobs/<id>/ws/ 下，附件相对路径会漂移（author_plus 事故口径）。
    """
    if entries:
        objective += "\n\n【目标入口】\n" + "\n".join(f"- {e}" for e in entries)
    if artifacts:
        objective += "\n\n【附件源码（已就位，用 read_file 读取）】\n" + "\n".join(
            f"- {a}" for a in artifacts
        )
    return objective


async def run_task(
    spec: TaskSpec,
    *,
    store: JobStore | None = None,
    llm=None,
    db_path: str = "var/secpt.sqlite",
    model_primary: str = "deepseek-chat",
    model_fallback: str = "deepseek-reasoner",
) -> dict:
    """统一路由：benchmark → campaign；其余 → frontier（唯一执行编排）。

    返回汇总 dict（frontier/benchmark 各自的 RunSummary），补 entries 维度
    （编排层不感知入口，entries 来自 TaskSpec）。
    """
    from secpt.core.orchestrator.frontier import run_frontier
    from secpt.core.profile.loader import load_profile
    from secpt.core.profile.router import _match_rules

    # 协议闸口（唯一漏斗，CLI/Web 两个入口都在这里过）：角色未指定时，题面命中
    # benchmark 平台关键词 → 升级 platform 协议（战役跑分）。角色/场景/经验的
    # 选择不是协议，不在此处理——由指挥官首轮唤醒完成。
    if spec.task_type in ("auto", "") and "benchmark" in _match_rules(spec.objective or ""):
        spec = spec.model_copy(update={"task_type": "benchmark"})

    if spec.task_type == "benchmark":
        from .benchmark import run_benchmark_campaign

        summary = await run_benchmark_campaign(spec, llm=llm, model=model_primary)
        summary["entries"] = list(spec.entries or [])
        return summary

    prof = load_profile(spec.task_type)
    objective = augment_objective(
        spec.objective, list(spec.entries or []), list(spec.artifacts or [])
    )
    assets = [e for e in (spec.entries or []) if e and e.strip()]

    kw: dict = dict(
        profile=spec.task_type,
        base_job_id=spec.job_id,
        llm=llm,
        model=model_primary,
        # auto 模式（角色交由指挥官首轮选定）：默认开 3 路操作员并行池，
        # 角色落定后各任务按选定 profile 跑；显式 profile 沿用其 workers 配置
        max_workers=(
            max(3, int(getattr(prof, "workers", 1) or 1))
            if spec.task_type == "auto"
            else max(1, int(getattr(prof, "workers", 1) or 1))
        ),
        default_assets=assets,
        default_max_steps=spec.budget.steps if spec.budget.steps > 0 else 0,
        # goal asset_scope 输入可信边界：entries + SECPT_SCOPE_ALLOW（双空=跳过校验）
        scope_allow=assets
        + [a for a in os.environ.get("SECPT_SCOPE_ALLOW", "").split(",") if a.strip()],
    )
    if spec.budget.wall_s > 0:
        # --wall 秒数 → 全局截止（frontier deadline_ts；0/-1 不限，7×24 语义不变）
        kw["deadline_ts"] = time.time() + float(spec.budget.wall_s)
        kw["reserve_s"] = min(300.0, float(spec.budget.wall_s) * 0.1)

    stages = list(getattr(prof, "stages", None) or [])
    if stages:
        # 阶段折叠：种子链 + 关闭 planner（清单型语义，同 benchmark）
        kw.update(stages=stages, max_intents=len(stages), stop_on_first_flag=False)
        # 线性 profile 链保持旧语义（阶段本身即清单），不叠加交付/后勤通道
        kw.update(enable_logistics=False, enable_delivery=False)
    else:
        # v3：不再预置角色骨架。多资产只决定是否持续跑完所有任务；
        # 任务拆分/独立性/后勤分流由 planner + triage 决定。
        multi = len(assets) > 1
        kw.update(role_plan=False, stop_on_first_flag=not multi)
        kw.update(
            enable_logistics=os.environ.get("SECPT_ORCH_LOGISTICS", "1") != "0",
            enable_delivery=os.environ.get("SECPT_ORCH_DELIVERY", "1") != "0",
            enable_commander=os.environ.get("SECPT_ORCH_COMMANDER", "1") != "0",
        )

    summary = await run_frontier(objective, **kw)
    summary["entries"] = list(spec.entries or [])
    return summary
