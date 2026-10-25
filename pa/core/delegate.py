"""简报编译（BUILD_SPEC §7.4）：把任务 + 相关状态压成 worker 的 payload。

禁止把父的对话历史、父的 system prompt 放进去（§7.4）。
untrusted_data 恒为空列表——由 worker 运行时累积（约束 A：dispatch 时为空）。
"""
from __future__ import annotations

from pa.models import MissionState, Task

RELEVANT_CLUES_K = 20


def compile_briefing(state: MissionState, task: Task, assets) -> dict:
    """worker 的初始 payload；clue_cursor 让 worker 知道自己在哪条水位上。"""
    relevant = [c.model_dump(mode="json") for c in state.clues[-RELEVANT_CLUES_K:]]
    return {
        "goal": task.goal,
        "type": task.type,
        "success_criteria": [c.model_dump(mode="json")
                             for c in task.success_criteria],
        "abort_criteria": list(task.abort_criteria),
        "scope_slice": dict(task.scope_slice),
        "allowed_tools": list(task.allowed_tools),
        "relevant_clues": relevant,
        "applicable_skills": [s.id for s in assets.skills_for(task.type)],
        "failed_attempts": max(task.attempt - 1, 0),
        "untrusted_data": [],                 # 约束 A：dispatch 时为空
        "clue_cursor": state.clue_seq,
    }
