"""流程日志与 worker 思考事件验收。

- Flow 渲染：planner → orchestrator → worker 思考/行动/观察 → 判定 →
  收口；同一账本两次渲染逐字节相等；
- worker 的 finish 决策必须进账本（decision.made），否则日志只能看到
  「它调了什么工具」，看不到「它为什么收口」。
"""

from __future__ import annotations

import json
from pathlib import Path

from pa.assets.snapshot import AssetSnapshot
from pa.core.worker import run_worker
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Budget,
    Clue,
    Criterion,
    Event,
    MissionSpec,
    MissionState,
    Task,
    now,
)
from pa.report.flow import Flow
from pa.store.writer import StoreWriter

ROOT = Path(__file__).resolve().parent.parent


def _spec() -> MissionSpec:
    return MissionSpec(
        objective="探测本地端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1",
                item="端口开放被证实",
                evidence="probe.open",
                target="127.0.0.1",
            )
        ],
        budget=Budget(steps=6, seconds=60, tokens=10_000),
        allowed_scope=["127.0.0.1:1"],
    )


def _task() -> Task:
    return Task(
        id="t-0001",
        mission_id="m",
        type="recon",
        goal="探测 127.0.0.1:1",
        success_criteria=[Criterion(evidence="probe.open")],
        allowed_tools=["probe"],
        budget=Budget(steps=4),
    )


def _ev(seq: int, type: str, payload: dict) -> Event:
    return Event(
        seq=seq,
        mission_id="m",
        ts=now(),
        type=type,
        actor="engine",
        payload=payload,
        idem_key=f"k{seq}",
    )


def _events() -> list[Event]:
    task = _task()
    clue = Clue(
        kind="probe.open",
        key="127.0.0.1:1",
        value="open",
        target="127.0.0.1",
        source_task="t-0001",
    )
    return [
        _ev(1, "mission.created", {"objective": "探测本地端口", "targets": ["127.0.0.1"]}),
        _ev(
            2,
            "context.assembled",
            {
                "phase": "planner",
                "total_chars": 4200,
                "sections": {"system": {"pct": 0.7}, "brief": {"pct": 0.2}},
            },
        ),
        _ev(3, "task.created", {"task": task.model_dump(mode="json"), "task_seq": 1}),
        _ev(
            4,
            "plan.created",
            {"n": 1, "strategy": {"focus": "先锚点"}, "rationale": "先探端口"},
        ),
        _ev(
            5,
            "decision.made",
            {
                "kind": "orchestrate",
                "round": 1,
                "mode": "orchestrator",
                "run": ["t-0001"],
                "created": [],
                "rationale": "先派锚点任务",
                "current_strategy": {"focus": "先锚点"},
            },
        ),
        _ev(6, "task.dispatched", {"task_id": "t-0001", "attempt": 1}),
        _ev(
            7,
            "action.started",
            {
                "tool": "probe",
                "args": {"target": "127.0.0.1", "port": 1},
                "fp": "fp1",
                "task_id": "t-0001",
                "expect": "端口开放",
                "rationale": "先探活",
            },
        ),
        _ev(
            8,
            "action.finished",
            {
                "tool": "probe",
                "fp": "fp1",
                "exit": 0,
                "evidence": None,
                "task_id": "t-0001",
            },
        ),
        _ev(9, "clue.added", {"clue": clue.model_dump(mode="json")}),
        _ev(
            10,
            "decision.made",
            {
                "kind": "finish",
                "task_id": "t-0001",
                "attempt": 1,
                "step": 1,
                "summary": "端口已确认",
                "rationale": "证据足够",
                "satisfied": ["acc-1"],
            },
        ),
        _ev(
            11,
            "task.state_changed",
            {
                "task_id": "t-0001",
                "from": "running",
                "to": "failed",
                "reason": "virtual_success",
                "attempt": 1,
            },
        ),
        _ev(12, "budget.charged", {"tokens": 1234, "seconds": 1}),
        _ev(
            13,
            "mission.finished",
            {
                "summary": "完成",
                "reason": "completed",
                "satisfied": ["acc-1"],
            },
        ),
    ]


def test_flow_renders_thinking_and_verdict(tmp_path):
    root = tmp_path / "m"
    root.mkdir()
    (root / "mission.json").write_text(_spec().model_dump_json(), encoding="utf-8")
    events = _events()
    (root / "events.jsonl").write_text(
        "\n".join(e.model_dump_json() for e in events) + "\n", encoding="utf-8"
    )

    text = Flow(root).render_all(events)
    assert "[planner] 制定首个计划" in text
    assert "[第 1 步] orchestrator 编排" in text
    assert "理由   先派锚点任务" in text
    assert "[思考] 预期: 端口开放" in text
    assert "[行动] probe 127.0.0.1:1" in text
    assert "[观察] exit=0" in text
    assert "[线索] probe.open" in text
    assert "[思考] 决定收口" in text
    assert "[判定] failed · virtual_success" in text
    assert "[收口] completed" in text
    assert "验收   1/1 达成" in text

    assert Flow(root).render_all(events) == text, "同一账本两次渲染必须一致"


def test_worker_finish_is_logged_as_decision(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    spec = _spec()
    (root / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
    state = MissionState(spec=spec, mission_id="w")
    task = _task()
    assets = AssetSnapshot.freeze(
        ROOT / "pa" / "core" / "prompts",
        [ROOT / "assets" / "skills", ROOT / "assets" / "playbooks"],
        [ROOT / "assets" / "roles"],
    )
    finish = json.dumps(
        {
            "kind": "finish",
            "summary": "已确认端口开放",
            "satisfied": ["acc-1"],
            "rationale": "probe 返回 open，判据已满足",
        },
        ensure_ascii=False,
    )
    writer = StoreWriter(root)
    result = run_worker(task, state, writer, MockLLM({"worker": [finish]}), assets)
    assert result.status == "done"

    events = list(writer.events_path.read_text(encoding="utf-8").splitlines())
    parsed = [json.loads(line) for line in events]
    decisions = [
        e
        for e in parsed
        if e["type"] == "decision.made" and e["payload"].get("kind") == "finish"
    ]
    assert decisions, "finish 决策必须进账本，否则日志看不到为什么收口"
    payload = decisions[0]["payload"]
    assert payload["summary"] == "已确认端口开放"
    assert payload["rationale"] == "probe 返回 open，判据已满足"
    assert payload["satisfied"] == ["acc-1"]
    assert payload["task_id"] == "t-0001"
    assert payload["attempt"] == 0
    assert payload["step"] == 1


def test_run_flow_does_not_change_ledger(tmp_path):
    """flow 只读：跑完后的账本 sha256 不变。"""
    import hashlib

    from pa.report import follow

    root = tmp_path / "m"
    root.mkdir()
    (root / "mission.json").write_text(_spec().model_dump_json(), encoding="utf-8")
    events = _events()
    ledger = root / "events.jsonl"
    ledger.write_text(
        "\n".join(e.model_dump_json() for e in events) + "\n", encoding="utf-8"
    )
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    import io

    out = io.StringIO()
    rc = follow.watch(root, out=out, poll=0.01, flow=True)
    assert rc == 0
    assert "[收口] completed" in out.getvalue()
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before
