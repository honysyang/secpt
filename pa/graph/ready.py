"""ready 集合与前置提升（BUILD_SPEC §6.3；ARCHITECTURE §8 L2）。

deps 是硬依赖（必须 done）；preconditions 是软依赖（由线索事实判定，
缺什么补什么的自动动作就来自这里）。
"""
from __future__ import annotations

from pa.graph.state import transition
from pa.models import MissionState, Precondition, Task


def _deps_done(state: MissionState, t: Task) -> bool:
    # 依赖指向不存在的任务按未满足处理，不让 KeyError 炸掉调度
    return all(d in state.tasks and state.tasks[d].state == "done"
               for d in t.deps)


def _precond_ok(state: MissionState, p: Precondition) -> bool:
    # match 的每一项都要求存在 key 与 value 都吻合的线索
    return all(any(c.key == k and c.value == v for c in state.clues)
               for k, v in p.match.items())


def ready_tasks(state: MissionState) -> list[Task]:
    out = []
    for t in state.tasks.values():
        if t.state not in ("pending", "ready"):
            continue
        if not _deps_done(state, t):
            continue
        if not all(_precond_ok(state, p) for p in t.preconditions):
            continue
        out.append(t)
    return sorted(out, key=lambda t: -t.priority)


def promote_pending(state: MissionState, writer) -> int:
    """把满足依赖与前置的 pending 任务提升为 ready，返回提升数量。"""
    n = 0
    for t in ready_tasks(state):
        if t.state == "pending":
            transition(t, "ready", writer)
            n += 1
    return n


def validate_dag(state: MissionState, writer) -> list[str]:
    """批量创建后检查 deps 悬空（裁定 3）：依赖指向不存在任务的任务置
    blocked(reason="dangling_dep")，并发一条 mentor.intervened(I3)。

    ready_tasks 对悬空依赖按未满足宽容处理（不 KeyError），但宽容只会让
    任务永远不就绪、表现为平台期——validate_dag 把它变成可诊断的 blocked
    + 介入事件。返回被置 blocked 的任务 id 列表。
    """
    dangling = [t.id for t in state.tasks.values()
                if t.state == "pending"
                and any(d not in state.tasks for d in t.deps)]
    for tid in dangling:
        transition(state.tasks[tid], "blocked", writer, reason="dangling_dep")
    if dangling:
        writer.append("mentor.intervened", "mentor",
                      {"level": "I3", "issue": "dangling_dep",
                       "detail": {"task_ids": dangling}},
                      f"dag:{'|'.join(dangling)}")
    return dangling


def revive_blocked(state: MissionState, writer) -> list[str]:
    """复活 blocked 任务（ARCHITECTURE §6.1 blocked→pending 边；scheduler
    每轮派发前调用，S10 接线）：

    - block_reason == "dangling_dep" 且所有 deps 已存在 → 转回 pending，
      block_reason 由 transition 清空；
    - 其余原因（out_of_scope / needs_approval 等）不自动复活。
    """
    revived = []
    for t in state.tasks.values():
        if t.state != "blocked" or t.block_reason != "dangling_dep":
            continue
        if all(d in state.tasks for d in t.deps):
            transition(t, "pending", writer)
            revived.append(t.id)
    return revived
