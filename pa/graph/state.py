"""任务状态机：唯一的转移表 + transition（ARCHITECTURE §6.1 / §8 L2）。

守卫：每次转移写一条 task.state_changed；非法转移抛 IllegalTransition，
而不是静默修正（§6.1）。done 是终态（用户裁定 1）：done 的产物是证据，
要作废的是 finding，不是任务。pending→blocked 为裁定 3 增加：
validate_dag 发现悬空依赖时经 transition 置 blocked，不走旁路。
"""
from __future__ import annotations

from pa.errors import IllegalTransition
from pa.models import Task

ALLOWED = {
    "pending":    {"ready", "blocked", "superseded"},
    "ready":      {"running", "superseded"},
    "running":    {"done", "failed", "blocked", "ready", "superseded"},
    "failed":     {"ready", "superseded"},
    "done":       set(),
    "blocked":    {"pending", "superseded"},
    "superseded": set(),
}

_TERMINAL = ("done", "failed", "blocked", "superseded")


def transition(task: Task, to: str, writer, *, lease=None, attempt=None,
               reason=None) -> None:
    """状态转移：先落事件，再改内存；租约生命周期统一在这里收口。

    - lease 参数：派发进入 running 时由 scheduler 传入；
    - reason 参数（裁定 3）：置 blocked 时的原因（如 dangling_dep），
      随事件落盘，重放可还原；离开 blocked 且无新原因时清空旧原因
      （复活路径，如 dangling_dep 修复后 revive_blocked 转回 pending）；
    - 终态或退回 ready（租约过期路径）一律清空租约——否则重启后过期租约会
      继续占着任务，任务永远不再被调度；
    - 事件携带 attempt / lease / reason，重放（apply_event）据此还原，保证
      live 与 restore 后的状态逐字段一致。
    """
    if to not in ALLOWED[task.state]:
        raise IllegalTransition(
            f"illegal transition {task.state} -> {to} (task {task.id})")
    if lease is not None:
        task.lease = lease
    elif to in _TERMINAL or to == "ready":
        task.lease = None
    if attempt is not None:
        task.attempt = attempt
    if reason is not None:
        task.block_reason = reason
    elif to != "blocked":
        task.block_reason = None
    writer.append("task.state_changed", "engine",
                  {"task_id": task.id, "from": task.state, "to": to,
                   "attempt": task.attempt,
                   "lease": task.lease.model_dump(mode="json") if task.lease else None,
                   "reason": reason},
                  f"ts:{task.id}:{to}:{task.attempt}")
    task.state = to
