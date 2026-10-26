"""调度器（ARCHITECTURE §8 L4）：一轮固定顺序 = 一个超级步。

裁定 C 的顺序，一轮内严格依次：
  revive_blocked → 失败重排队（带上界） → 选 ready → 派发 → merge(tried 并回)
  → judge → 状态落定 → JEV sweep。

JEV sweep 只做事编排（找未裁决 finding → 调 pa/adjudicate 的规则 → 落账），
裁决规则本身在 pa/adjudicate/rules.py，可单测（S11 约束 A）。

失败重排队上界（§6.2 裁定）：failed → ready 仅当 attempt < MAX_ATTEMPTS，
达到上界停在 failed——否则无效重试会把平台期永远挡在「还有 ready 任务」
之外，只能靠预算收口。
"""
from __future__ import annotations

from pa.adjudicate import adjudicate, finding_id
from pa.core.reflect import judge
from pa.core.worker import run_worker
from pa.graph.ready import promote_pending, ready_tasks, revive_blocked
from pa.graph.state import transition
from pa.models import Adjudication, MissionState
from pa.policy import coach
from pa.runtime.lease import make_lease
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

MAX_ATTEMPTS = 3                       # 单任务最大尝试次数（§6.2 裁定 1）


def round(state: MissionState, writer: StoreWriter, llm, assets) -> None:
    # 1) 复活 dangling_dep 阻塞任务
    revive_blocked(state, writer)
    # 1.5) 失败重排队：仅 attempt < MAX_ATTEMPTS；restart.decided 落账，
    #      restarts_same_task 由 derive_stats 重算
    for t in state.tasks.values():
        if t.state == "failed" and t.attempt < MAX_ATTEMPTS:
            transition(t, "ready", writer)
            writer.append("restart.decided", "engine",
                          {"task_id": t.id, "attempt": t.attempt},
                          f"rs:{t.id}:{t.attempt}")
    # 2) 选 ready：提升 pending → 按优先级取 ready 批
    promote_pending(state, writer)
    batch = [t for t in ready_tasks(state) if t.state == "ready"]
    # max_workers：v0 worker 同步执行，批内顺序派发；截断语义留给将来真并发
    batch = batch[: max(1, state.spec.max_workers)]
    for task in batch:
        _dispatch(task, state, writer, llm, assets)
    # 7) JEV sweep：本轮新产生的 + 历轮漏判的 finding 一并裁决（规则在
    #    pa/adjudicate，这里只编排与落账）
    jev_sweep(state, writer)
    # 8) 行为参数本轮重算（裁定 1.3：judge 之后、coach 之前——coach 的
    #    advisory.metric 必须含本轮失败，不得读上一轮的陈旧 meta）
    from pa.store.derive import derive_stats
    from pa.store.view import StoreView
    state.meta.update(derive_stats(StoreView(writer.root).events(from_seq=1)))
    # 9) 教练检查：shadow 只 advise，enforce 追加三个动作之一
    coach.on_round(state, writer)


def _dispatch(task, state: MissionState, writer: StoreWriter, llm, assets) -> None:
    attempt = task.attempt + 1
    transition(task, "running", writer, lease=make_lease(task), attempt=attempt)
    writer.append("task.dispatched", "engine",
                  {"task_id": task.id, "attempt": attempt},
                  f"td:{task.id}:{attempt}")
    result = run_worker(task, state, writer, llm, assets)
    writer.append("worker.returned", f"worker:{task.id}",
                  {"task_id": task.id, "status": result.status,
                   "failure": (result.failure.model_dump(mode="json")
                               if result.failure else None)},
                  f"wr:{task.id}:{attempt}")
    # 3) merge(tried 并回)：跨尝试继承，保序去重
    seen: set[str] = set()
    merged: list[str] = []
    for fp in list(task.tried) + list(result.tried):
        if fp not in seen:
            seen.add(fp)
            merged.append(fp)
    task.tried = merged
    # 4) judge（status=done 只是主张）；5) 状态落定（CONTROL_PLANE §6.2 矩阵）
    verdict = judge(task, result, state)
    if verdict in ("success", "false_failure"):
        transition(task, "done", writer)
    elif verdict == "virtual_success":
        transition(task, "failed", writer, reason="virtual_success")
    elif verdict == "blocked":
        transition(task, "blocked", writer,
                   reason=result.failure.detail if result.failure else "blocked")
    else:                                    # failure / partial → failed
        transition(task, "failed", writer,
                   reason=result.failure.cls if result.failure else "partial")


def jev_sweep(state: MissionState, writer: StoreWriter) -> list[str]:
    """裁决所有未裁决的 finding，返回本轮新裁决的 finding_id 列表。"""
    from pa.store.view import StoreView
    view = StoreView(writer.root)
    done: list[str] = []
    for f in state.findings:
        fid = finding_id(f)
        if fid in state.adjudications:
            continue
        verdict, reason = adjudicate(view, f)
        adj = Adjudication(finding_id=fid, verdict=verdict, reason=reason,
                           verdict_by="jev-v0")
        ev = writer.append("finding.adjudicated", "engine",
                           adj.model_dump(mode="json"), f"adj:{fid}")
        apply_event(state, ev)
        done.append(fid)
    return done
