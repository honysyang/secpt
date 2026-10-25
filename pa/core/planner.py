"""首个计划（BUILD_SPEC §7.3）：planner phase 产 PlanDraft，落成任务图。

bootstrap_plan 只在 mission 引导时调用一次；plan.created 事件的幂等键
带 mission id，崩溃重入不会产生第二个计划。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from pa.core.context import assemble
from pa.graph.task import create_task
from pa.infra.llm import complete_retry
from pa.models import MissionState, TaskDraft
from pa.store.writer import StoreWriter


class PlanDraft(BaseModel):
    tasks: list[TaskDraft]
    current_strategy: dict = Field(default_factory=dict)
    rationale: str = ""


def bootstrap_plan(state: MissionState, writer: StoreWriter, llm, assets) -> None:
    """引导首个计划：planner 决策 → create_task 逐个落账 → plan.created。
    current_strategy 随事件落盘（apply_event 重放恢复）。"""
    system, user = assemble("planner", state, {}, assets)
    raw = complete_retry(llm, retries=1, system=system, user=user,
                         schema=PlanDraft, phase="planner")
    plan = llm.parse(raw, PlanDraft)
    for d in plan.tasks:
        create_task(state, writer, d)
    state.current_strategy = plan.current_strategy
    writer.append("plan.created", "core",
                  {"n": len(plan.tasks),
                   "strategy": plan.current_strategy,
                   "rationale": plan.rationale},
                  f"plan:{state.mission_id}:1")
