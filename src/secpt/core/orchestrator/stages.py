"""多阶段渗透编排：profile.stages 顺序执行，共享 workspace。

设计（对照 HPTSA/VulnBot 的可迁移最小实现）：
- 每阶段 = 一次完整 Engine 运行（独立 job_id <job_id>-<stage>，可单独 resume/tail）
- 共享工作目录：所有阶段的 workspace 指向 var/jobs/<job_id>/（work_dir=…/ws 同一份），
  阶段产物（脚本/利用代码/凭据文件）自然跨阶段可见
- 交接：上一阶段的 ANSWER/收尾内容以【上一阶段结果】注入下一阶段 system
- 任一阶段拿到最终 ANSWER 即整体成功；全部阶段结束未拿到 → 最后阶段状态
- 数据驱动：阶段定义全在 profile YAML，零新执行器
"""

from __future__ import annotations

import asyncio

from secpt.core.job.models import TaskSpec
from secpt.core.job.store import JobStore
from secpt.core.orchestrator.summary import ChainResult, RunSummary
from secpt.core.profile.loader import Profile


async def run_stages(
    spec: TaskSpec,
    profile: Profile,
    *,
    store: JobStore | None = None,
    llm=None,
    db_path: str = "var/secpt.sqlite",
    model_primary: str = "deepseek-chat",
    model_fallback: str = "deepseek-reasoner",
) -> dict:
    """按 profile.stages 顺序执行。返回汇总 dict（与 run_task 字段兼容）。"""
    from secpt.cli.engine_runner import run_engine
    from secpt.core.job.resume import jobs_dir

    store = store or JobStore(db_path)
    base = jobs_dir() / spec.job_id
    base.mkdir(parents=True, exist_ok=True)

    chains: list[dict] = []
    handoff = ""
    final_result = None
    total_wall = 0.0

    entries = list(spec.entries or [])
    base_objective = spec.objective + (
        "\n\n【目标入口】\n" + "\n".join(f"- {e}" for e in entries) if entries else ""
    )

    for idx, stage in enumerate(profile.stages):
        is_last = idx == len(profile.stages) - 1
        stage_job_id = f"{spec.job_id}-{stage.name}"

        objective = base_objective + f"\n\n【当前阶段：{stage.name}】{stage.role}"
        if stage.stop_condition:
            objective += f"\n阶段收尾要求：{stage.stop_condition}"
        if handoff:
            objective += (
                f"\n\n【上一阶段（{profile.stages[idx - 1].name}）结果】\n{handoff}\n"
                f"共享工作目录：{(base / 'ws').resolve()}（已有上一阶段产物）"
            )

        # 阶段预算：0 值回退 profile 总预算
        max_steps = stage.budget.steps or profile.budget.steps
        max_wall_s = stage.budget.wall_s or profile.budget.wall_s

        print(
            f"[stages] === stage {idx + 1}/{len(profile.stages)}: {stage.name} "
            f"(job={stage_job_id}, steps<={max_steps}, wall<={max_wall_s}s) ==="
        )
        result = await asyncio.to_thread(
            run_engine,
            objective=objective,
            profile=spec.task_type,
            prior=spec.prior,
            job_id=stage_job_id,
            workspace=str(base),
            job_store=store,
            llm=llm,
            max_steps=max_steps,
            max_wall_s=max_wall_s,
            model_primary=model_primary,
            model_fallback=model_fallback,
            stage=stage,
            artifacts=spec.artifacts,
        )
        final_result = result
        total_wall += result.wall_s
        chains.append(
            ChainResult(
                role=stage.name,
                entry=-1,  # 阶段无入口维度（入口并入 objective）
                flag=result.flag,
                steps=result.steps,
                wall_s=round(result.wall_s, 1),
                status=result.status,
            )
        )

        # 交接摘要：最后一条 assistant 正文（含该阶段 ANSWER 交付物）
        handoff = ""
        for m in reversed(result.messages):
            if m.get("role") == "assistant" and (m.get("content") or "").strip():
                handoff = str(m["content"]).strip()[:1500]
                break

        # 只有最终阶段的 ANSWER 才算整体成果；中间阶段的 ANSWER 是阶段交付物（交接）
        if result.flag and is_last:
            break

    return RunSummary(
        job_id=spec.job_id,
        mode="stages",
        entries=entries,
        flags=[final_result.flag] if final_result and final_result.flag else [],
        chains=chains,
        wall_s=round(total_wall, 1),
        extras={"recon_assets": 0},  # 历史键保留兼容
    ).to_dict()
