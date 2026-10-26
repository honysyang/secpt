"""简报编译（BUILD_SPEC §7.4）：把任务 + 相关状态压成 worker 的 payload。

禁止把父的对话历史、父的 system prompt 放进去（§7.4）。
untrusted_data 恒为空列表——由 worker 运行时累积（约束 A：dispatch 时为空）。
"""

from __future__ import annotations

import json

from pa.core import experience
from pa.models import MissionState, Task

RELEVANT_CLUES_K = 20


def _recent_actions(root, task_id: str, limit: int = 12) -> list[str]:
    """从账本取本任务最近的动作（tool + args 摘要），供 worker 避免重试。

    task.tried 只是指纹，模型看不懂；这里给人类可读的已试动作。读账本
    只读、不改，回放/恢复后同样可用。
    """
    if root is None:
        return []
    from pa.store.view import StoreView

    out: list[str] = []
    for ev in reversed(list(StoreView(root).events())):
        if ev.type != "action.started":
            continue
        p = ev.payload
        if str(p.get("task_id") or "") != task_id:
            continue
        args = json.dumps(p.get("args") or {}, ensure_ascii=False, sort_keys=True)
        out.append(f"{p.get('tool')} {args[:180]}")
        if len(out) >= limit:
            break
    return list(reversed(out))


def _last_failure(root, task_id: str) -> dict | None:
    """上一次尝试的失败原因（Reflexion-lite）：重派任务时 worker 要知道
    「上次为什么失败」，否则同一失败模式会重复三次（真跑 t-0001/t-0007
    教训：nslookup×3、同款汇总×3）。取最近一条 worker.returned 的
    failure；最近一次成功（done）则不注入——成功后不该背着旧包袱。"""
    if root is None:
        return None
    from pa.store.view import StoreView

    for ev in reversed(list(StoreView(root).events())):
        if ev.type != "worker.returned":
            continue
        if str(ev.payload.get("task_id") or "") != task_id:
            continue
        f = ev.payload.get("failure")
        if not f:
            return None
        return {
            "cls": str(f.get("cls") or "?"),
            "detail": str(f.get("detail") or "")[:200],
        }
    return None


def compile_briefing(state: MissionState, task: Task, assets, root=None) -> dict:
    """worker 的初始 payload；clue_cursor 让 worker 知道自己在哪条水位上。

    root 可选（向后兼容）：给出时按任务类型注入长期经验条目（E3，
    进入 payload["lessons"] 渲染管道；空值不输出——库为空或无命中时
    section 整段缺席，旧行为逐字节不变）。worker 的写路径永不触碰
    经验库——只读继承纪律（L249）在这里与 distill 分界。"""
    relevant = [c.model_dump(mode="json") for c in state.clues[-RELEVANT_CLUES_K:]]
    lessons = experience.lessons_for(root, [task.type]) if root is not None else []
    payload = {
        "goal": task.goal,
        "type": task.type,
        "role": task.role,  # M1：角色 id（空=未指派），context 注入手册
        "success_criteria": [c.model_dump(mode="json") for c in task.success_criteria],
        "abort_criteria": list(task.abort_criteria),
        "scope_slice": dict(task.scope_slice),
        "allowed_tools": list(task.allowed_tools),
        "relevant_clues": relevant,
        "lessons": lessons,  # E3 长期经验（只读继承）
        "applicable_skills": [
            s.id for s in assets.skills_for(task.type, role=task.role)
        ],
        "failed_attempts": max(task.attempt - 1, 0),
        # 已试动作指纹：只给最近 12 个，避免跨尝试重走同一条路。
        # worker 侧提示词会明确要求「相同 tool+args 不得重复」。
        "tried": list(task.tried)[-12:],
        "tried_actions": _recent_actions(root, task.id),
        # 附件清单（workspace 相对路径）：0day 研究任务的审计对象
        "files": [f.name for f in state.spec.files],
        "untrusted_data": [],  # 约束 A：dispatch 时为空
        "clue_cursor": state.clue_seq,
    }
    last_failure = _last_failure(root, task.id) if root is not None else None
    if last_failure is not None:
        payload["last_failure"] = last_failure
    return payload
