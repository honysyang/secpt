"""S3：状态机 / 租约清理 / ready 集合（ARCHITECTURE §6.1、§8 L2；追加验收）。

追加验收：transition() 清空租约的四条路径 to ∈ {done, failed, blocked,
superseded} 全部测到——否则重启后过期租约会继续占着任务，任务永远不再被调度。
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from pa.errors import IllegalTransition
from pa.graph.ready import promote_pending, ready_tasks, validate_dag
from pa.graph.state import transition
from pa.graph.task import create_task
from pa.models import (
    AcceptanceItem, Budget, Clue, Criterion, Lease, MissionSpec, MissionState,
    Precondition, Task, TaskDraft,
)
from pa.store.view import StoreView
from pa.store.writer import StoreWriter


def _lease() -> Lease:
    from pa.models import now
    return Lease(owner="w-1", expires=now() + timedelta(seconds=60))


def _mk(tid: str, **kw) -> Task:
    base = dict(id=tid, mission_id="m", type="recon", goal="探活",
                success_criteria=[Criterion(evidence="port.open")],
                scope_slice={}, allowed_tools=["probe"])
    base.update(kw)
    return Task(**base)


def _state(tasks: list[Task], mid: str = "m") -> MissionState:
    spec = MissionSpec(objective="o", targets=[],
                       acceptance=[AcceptanceItem(id="a1", item="i", evidence="e")],
                       budget=Budget(), allowed_scope=[])
    return MissionState(spec=spec, mission_id=mid, tasks={t.id: t for t in tasks})


def _draft() -> TaskDraft:
    return TaskDraft(type="recon", goal="探活",
                     success_criteria=[Criterion(evidence="port.open")],
                     scope_slice={}, allowed_tools=["probe"])


def test_illegal_transitions_raise(tmp_mission):
    w = StoreWriter(tmp_mission)
    cases = [("pending", "running"), ("done", "ready"), ("done", "superseded"),
             ("superseded", "ready"), ("blocked", "running"), ("ready", "done")]
    for frm, to in cases:
        t = _mk("t1")
        t.state = frm
        with pytest.raises(IllegalTransition, match=frm):
            transition(t, to, w)
        assert t.state == frm          # 非法转移不改状态、不落事件
    assert not (tmp_mission / "events.jsonl").exists()


def test_transition_clears_lease_on_terminal_paths(tmp_mission):
    """追加验收：done / failed / blocked / superseded 四条路径租约全部清空。"""
    w = StoreWriter(tmp_mission)
    for to in ("done", "failed", "blocked", "superseded"):
        t = _mk(f"t-{to}")
        t.state = "running"
        t.lease = _lease()
        transition(t, to, w)
        assert t.lease is None, f"to={to} 租约未清空"
    # 退回 ready（租约过期路径）同样清空
    t = _mk("t-back")
    t.state = "running"
    t.lease = _lease()
    transition(t, "ready", w)
    assert t.lease is None


def test_transition_replay_restores_lease_and_attempt(tmp_mission):
    _write_mission(tmp_mission)
    w = StoreWriter(tmp_mission)
    st = _state([])
    t = create_task(st, w, _draft())
    assert t.id == "t-0001" and t.state == "pending"
    transition(t, "ready", w)
    transition(t, "running", w, lease=_lease(), attempt=1)
    live = StoreView(tmp_mission).restore()
    rt = live.tasks["t-0001"]
    assert rt.state == "running" and rt.attempt == 1
    assert rt.lease is not None and rt.lease.owner == "w-1"
    transition(rt, "failed", w)
    again = StoreView(tmp_mission).restore()
    ft = again.tasks["t-0001"]
    assert ft.state == "failed" and ft.lease is None and ft.attempt == 1


def _write_mission(root) -> None:
    spec = MissionSpec(objective="o", targets=[],
                       acceptance=[AcceptanceItem(id="a1", item="i", evidence="e")],
                       budget=Budget(), allowed_scope=[])
    (root / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")


def test_ready_set_and_promote(tmp_mission):
    w = StoreWriter(tmp_mission)
    t1 = _mk("t1")                                                  # 无依赖 → ready
    t2 = _mk("t2", deps=["t1"], priority=0.6)                       # 依赖未完成
    t3 = _mk("t3", priority=0.4,
             preconditions=[Precondition(match={"port": 445})])     # 缺线索
    t4 = _mk("t4", priority=0.9)
    t5 = _mk("t5", deps=["ghost"])                                  # 依赖不存在
    st = _state([t1, t2, t3, t4, t5])
    assert [t.id for t in ready_tasks(st)] == ["t4", "t1"]
    assert promote_pending(st, w) == 2
    assert st.tasks["t1"].state == "ready" and st.tasks["t4"].state == "ready"
    assert promote_pending(st, w) == 0            # 已提升的不重复
    # 补前置：线索到位 → t3 就绪
    st.clues.append(Clue(kind="port", key="port", value=445))
    assert [t.id for t in ready_tasks(st)] == ["t4", "t1", "t3"]
    # 依赖完成：t1 done → t2 就绪；t5 始终不就绪
    st.tasks["t1"].state = "done"
    assert [t.id for t in ready_tasks(st)] == ["t4", "t2", "t3"]


def test_create_task_replay(tmp_mission):
    _write_mission(tmp_mission)
    w = StoreWriter(tmp_mission)
    st = _state([], mid=tmp_mission.name)
    t1 = create_task(st, w, _draft())
    t2 = create_task(st, w, _draft())
    assert (t1.id, t2.id) == ("t-0001", "t-0002")
    assert st.task_seq == 2
    state = StoreView(tmp_mission).restore()
    assert set(state.tasks) == {"t-0001", "t-0002"}
    assert all(t.state == "pending" for t in state.tasks.values())
    assert state.tasks["t-0001"].mission_id == tmp_mission.name
    assert state.task_seq == 2                       # 计数器随事件重放恢复
    w2 = StoreWriter(tmp_mission)                    # 模拟重启后再创建
    t3 = create_task(state, w2, _draft())
    assert t3.id == "t-0003"                         # 不撞号（裁定 4）


def test_validate_dag_blocks_dangling_deps(tmp_mission):
    """裁定 3：悬空依赖置 blocked(reason="dangling_dep") + mentor.intervened(I3)。"""
    _write_mission(tmp_mission)
    w = StoreWriter(tmp_mission)
    st = _state([], mid=tmp_mission.name)
    t1 = create_task(st, w, _draft())
    draft2 = _draft().model_copy(update={"deps": [t1.id, "ghost"]})
    t2 = create_task(st, w, draft2)
    create_task(st, w, _draft())                     # t-0003 无依赖，不受影响

    from pa.graph.ready import validate_dag
    assert validate_dag(st, w) == [t2.id]
    assert st.tasks[t2.id].state == "blocked"
    assert st.tasks[t2.id].block_reason == "dangling_dep"
    assert st.tasks[t1.id].state == "pending"

    lines = [json.loads(x) for x in
             (tmp_mission / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    iv = [e for e in lines if e["type"] == "mentor.intervened"]
    assert len(iv) == 1
    assert iv[0]["payload"] == {"level": "I3", "issue": "dangling_dep",
                                "detail": {"task_ids": [t2.id]}}

    restored = StoreView(tmp_mission).restore()      # 重放后诊断信息不丢
    rt = restored.tasks[t2.id]
    assert rt.state == "blocked" and rt.block_reason == "dangling_dep"
    assert validate_dag(restored, StoreWriter(tmp_mission)) == []  # 幂等


def test_revive_blocked(tmp_mission):
    """裁定 2：dangling_dep 修复后可复活回 pending；其余原因不自动复活。"""
    _write_mission(tmp_mission)
    w = StoreWriter(tmp_mission)
    st = _state([], mid=tmp_mission.name)
    t1 = create_task(st, w, _draft())
    draft2 = _draft().model_copy(update={"deps": [t1.id, "ghost"]})
    t2 = create_task(st, w, draft2)
    assert validate_dag(st, w) == [t2.id]

    from pa.graph.ready import revive_blocked
    assert revive_blocked(st, w) == []               # ghost 还没建出来
    draft3 = _draft().model_copy(update={"deps": [t1.id]})
    t3 = create_task(st, w, draft3)                  # 补上 ghost 这个任务
    st.tasks["ghost"] = st.tasks.pop(t3.id)
    st.tasks["ghost"].id = "ghost"
    assert revive_blocked(st, w) == [t2.id]
    assert st.tasks[t2.id].state == "pending"
    assert st.tasks[t2.id].block_reason is None      # 复活即清原因

    restored = StoreView(tmp_mission).restore()
    assert restored.tasks[t2.id].state == "pending"
    assert restored.tasks[t2.id].block_reason is None

    # 非 dangling_dep 原因不复活
    st.tasks[t2.id].state = "blocked"
    st.tasks[t2.id].block_reason = "out_of_scope"
    assert revive_blocked(st, w) == []
    assert st.tasks[t2.id].state == "blocked"
