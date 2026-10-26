"""首个计划（BUILD_SPEC §7.3）：planner phase 产 PlanDraft，落成任务图。

bootstrap_plan 只在 mission 引导时调用一次；plan.created 事件的幂等键
带 mission id，崩溃重入不会产生第二个计划。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from pa.core.context import assemble, section_sizes
from pa.graph.ready import validate_dag
from pa.graph.task import create_task
from pa.infra.llm import complete_retry
from pa.models import MissionState, TaskDraft
from pa.policy import coach, review
from pa.store.writer import StoreWriter


class PlanDraft(BaseModel):
    tasks: list[TaskDraft]
    current_strategy: dict = Field(default_factory=dict)
    rationale: str = ""


def bootstrap_plan(state: MissionState, writer: StoreWriter, llm, assets) -> None:
    """引导首个计划：planner 决策 → coach 结构检查 → create_task → plan.created。
    current_strategy 随事件落盘（apply_event 重放恢复）。

    planner phase 也发 context.assembled（S13 裁定 3）——同样只记尺寸。"""
    system, user = assemble("planner", state, {}, assets)
    writer.append("context.assembled", "planner",
                  {"task_id": "-", "attempt": 0, "step": 0, "phase": "planner",
                   **section_sizes("planner", state, {}, assets)},
                  f"ctx:plan:{state.mission_id}:1")
    raw = complete_retry(llm, retries=1, system=system, user=user,
                         schema=PlanDraft, phase="planner")
    plan = llm.parse(raw, PlanDraft)
    # P0-2：planner 的依赖编排一律不采用——v0 任务全部并列，可解析的
    # deps 直接置空（保留 deps 只会制造「依赖未完成 → 永不派发」的静默
    # 死锁）。悬空 deps（指向不存在的任务）保留下来交给 validate_dag
    # 诊断：置 blocked(dangling_dep) + mentor.intervened(I3)，绝不留在
    # pending 里空等。
    known = set(state.tasks) | review.batch_ids(state.task_seq,
                                                len(plan.tasks))
    for d in plan.tasks:
        if all(x in known for x in d.deps):
            d.deps = []
    for d in coach.on_plan(state, writer, plan.tasks):
        create_task(state, writer, d)
    # 兜底：create_task 后立即校验任务图（validate_dag 幂等，重放安全）
    validate_dag(state, writer)
    state.current_strategy = plan.current_strategy
    writer.append("plan.created", "core",
                  {"n": len(plan.tasks),
                   "strategy": plan.current_strategy,
                   "rationale": plan.rationale},
                  f"plan:{state.mission_id}:1")
