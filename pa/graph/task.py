"""任务创建（ARCHITECTURE §8 L2）。

id 用 state.task_seq 单调计数器生成（裁定 4）：序号随 task.created 事件
落盘（payload.task_seq），重放时 apply_event 据此恢复计数器——不用
len(state.tasks)+1，并发创建、任务归档、重放顺序不一致都会撞号。
"""
from __future__ import annotations

from pa.models import MissionState, Task, TaskDraft


def create_task(state: MissionState, writer, draft: TaskDraft) -> Task:
    state.task_seq += 1
    task = Task(
        id=f"t-{state.task_seq:04d}",
        mission_id=state.mission_id,
        type=draft.type,
        goal=draft.goal,
        parent=draft.parent,
        deps=list(draft.deps),
        preconditions=list(draft.preconditions),
        success_criteria=list(draft.success_criteria),
        abort_criteria=list(draft.abort_criteria),
        scope_slice=draft.scope_slice,
        allowed_tools=list(draft.allowed_tools),
        budget=draft.budget,
        priority=draft.priority,
    )
    writer.append("task.created", "engine",
                  {"task": task.model_dump(mode="json"),
                   "task_seq": state.task_seq}, f"tc:{task.id}")
    state.tasks[task.id] = task
    return task
