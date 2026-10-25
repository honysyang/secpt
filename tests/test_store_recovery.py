"""T1 账本幂等 + restore 重建 + meta 重算（BUILD_SPEC §15 T1；裁定 2 追加验收）。"""
from __future__ import annotations

import hashlib
import json

from pa.models import AcceptanceItem, Budget, Criterion, MissionSpec, Task
from pa.store.view import StoreView
from pa.store.writer import StoreWriter


def _write_mission_json(root) -> None:
    spec = MissionSpec(
        objective="评估暴露面", targets=["10.0.0.5:80"],
        acceptance=[AcceptanceItem(id="a1", item="端口清单", evidence="port.open")],
        budget=Budget(), allowed_scope=["10.0.0.0/24:80"],
    )
    (root / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")


def _task(mid: str, tid: str = "t1") -> Task:
    return Task(id=tid, mission_id=mid, type="recon", goal="探活",
                success_criteria=[Criterion(evidence="port.open")],
                scope_slice={}, allowed_tools=["probe"])


def _seed_progress(w: StoreWriter) -> None:
    """3 条线索 + 3 个超级步结算 + 计划/重启/虚报事件。"""
    for i in range(3):
        w.append("clue.added", "worker:w1",
                 {"clue": {"kind": "host", "key": f"h{i}", "value": "10.0.0.5"}},
                 f"clue:{i}")
    for i in range(3):
        w.append("budget.charged", "engine", {"tokens": 10, "seconds": 1}, f"bc:{i}")
    w.append("plan.created", "core", {"n": 3, "strategy": {}}, "plan:m:1")
    w.append("plan.revised", "core", {"ops": []}, "plan:m:2")
    for i in (1, 2):
        w.append("task.state_changed", "engine",
                 {"task_id": "t9", "from": "running", "to": "ready",
                  "reason": "lease_expired"}, f"te:t9:{i}")
    w.append("task.state_changed", "engine",
             {"task_id": "t8", "from": "running", "to": "failed",
              "reason": "virtual_success"}, "tv:t8")


_EXPECT_META = {
    "no_progress_steps": 2,        # 3 条 clue 之后连续 3 步无新线索：0,1,2
    "plan_rewrites": 1,
    "restarts_same_task": 2,
    "virtual_success_count": 1,
    "worker_returns": 0,
    "failures_by_class": {},
    "planned": 3,
    "fp_attempts": {},             # §6.5.1：无 action.finished 失败记录
}


def test_event_log_monotonic_and_idempotent(tmp_mission):
    w = StoreWriter(tmp_mission)
    w.append("clue.added", "engine", {"k": "v"}, "k1")
    w.append("clue.added", "engine", {"k": "v"}, "k1")   # 同 idem_key
    lines = (tmp_mission / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["seq"] == 1


def test_restore_rebuilds_tasks(tmp_mission):
    _write_mission_json(tmp_mission)
    w = StoreWriter(tmp_mission)
    w.append("task.created", "engine",
             {"task": _task(tmp_mission.name).model_dump(mode="json")}, "tc:t1")
    w.append("task.state_changed", "engine",
             {"task_id": "t1", "from": "pending", "to": "ready"}, "ts:t1:ready")
    state = StoreView(tmp_mission).restore()
    assert set(state.tasks) == {"t1"}
    assert state.tasks["t1"].state == "ready"
    assert state.spec.acceptance[0].id == "a1"
    assert state.mission_id == tmp_mission.name


def test_meta_survives_kill(tmp_mission):
    """裁定 2 追加验收一：写事件后 kill -9（等价于丢弃全部内存、新实例重放）。"""
    _write_mission_json(tmp_mission)
    w = StoreWriter(tmp_mission)
    for i in range(3):
        w.append("clue.added", "worker:w1",
                 {"clue": {"kind": "host", "key": f"h{i}", "value": "10.0.0.5"}},
                 f"clue:{i}")
    for i in range(3):
        w.append("budget.charged", "engine", {"tokens": 10, "seconds": 1}, f"bc:{i}")
    state = StoreView(tmp_mission).restore()
    assert state.meta["no_progress_steps"] == 2
    assert len(state.clues) == 3
    assert state.spent_tokens == 30
    assert state.spent_seconds == 3


def test_meta_replay_without_snapshot(tmp_mission):
    """裁定 2 追加验收二：删掉 snapshot.json，仅靠事件重放，meta 重建一致。"""
    _write_mission_json(tmp_mission)
    w = StoreWriter(tmp_mission)
    _seed_progress(w)
    w.snapshot_now(StoreView(tmp_mission).restore())
    assert StoreView(tmp_mission).restore().meta == _EXPECT_META   # 走快照路径
    (tmp_mission / "snapshot.json").unlink()                       # 删快照
    assert StoreView(tmp_mission).restore().meta == _EXPECT_META   # 仅事件重放


def test_writer_resume_seq_and_actions(tmp_mission):
    """重启后 seq 续接、idem 去重仍然生效、动作指纹索引可查。"""
    _write_mission_json(tmp_mission)
    w = StoreWriter(tmp_mission)
    w.append("action.started", "gateway", {"fp": "fp1"}, "act:fp1")
    w.append("action.finished", "gateway", {"fp": "fp1", "evidence": "ev-x"}, "fin:fp1")
    w2 = StoreWriter(tmp_mission)          # 模拟重启：重新扫描账本
    assert w2._seq == 2
    ev = w2.append("clue.added", "engine", {"k": "v"}, "act:fp1")   # 同 idem_key
    assert ev.seq == 1                                    # 去重，不新增
    w2.append("clue.added", "engine", {"k": "v"}, "clue:1")
    assert w2._seq == 3
    assert w2.seen_action("fp1") and w2.action_evidence("fp1") == "ev-x"


def test_evidence_view_layout(tmp_mission):
    """锁定证据库布局契约（S4 的 evidence.put 必须按此写入）。"""
    content = {"tool": "probe", "stdout": "open"}
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    eid = "ev-" + sha[:16]
    d = tmp_mission / "evidence" / sha[:2]
    d.mkdir(parents=True)
    (d / f"{sha}.json").write_bytes(raw)
    (d / f"{sha}.meta.json").write_text(
        json.dumps({"id": eid, "sha": sha, "task_id": "t1"}), encoding="utf-8")
    v = StoreView(tmp_mission)
    assert v.evidence(eid) == content
    assert v.evidence_meta(eid)["sha"] == sha
    assert v.evidence_verified(eid) is True
    assert v.evidence_verified("ev-" + "0" * 16) is False
