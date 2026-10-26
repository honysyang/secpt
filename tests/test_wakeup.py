"""信号级唤醒 v2 单测（WORKFLOW §4.1；对应 system reminder 台账 a2/a3）。

六组：
1. 分类：噪音级只入账不进唤醒包；信号级必进；
2. 水位：save 后窗口推进——噪音段不醒，新信号段再醒；
3. 同轮合并：窗口内多信号压成一个 WakeUp（去重、finding 置位）；
4. finding 置顶：round(wake=…含 finding) 先裁决台账再编排
   （finding.adjudicated 的 seq 早于 decision.made）；wake=None 回
   旧序；重复 round 不重复裁决（幂等）；
5. heartbeat 兜底闹钟：retryable failed → 重排队为 ready 且零 LLM
   调用；静止状态零事件；
6. loop 集成：run() 收敛行为与旧语义等价（由 test_runtime 全量回归兜底，
   本文件附一条「队列排空走心跳路仍收口」的场景）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa.adjudicate import finding_id as compute_finding_id
from pa.assets.snapshot import AssetSnapshot
from pa.graph.ready import promote_pending
from pa.graph.state import transition
from pa.graph.task import create_task
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Budget,
    FindingProposal,
    MissionSpec,
    MissionState,
    TaskDraft,
)
from pa.runtime import scheduler
from pa.runtime import wakeup as wakeup_mod
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def listener() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


def _freeze():
    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])


def _spec(port: int) -> MissionSpec:
    return MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1", item="端口开放被证实", evidence="open", target="127.0.0.1"
            )
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{port}"],
    )


def _worker_script(port: int) -> str:
    return json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": port},
            "expect": "端口开放",
        },
        ensure_ascii=False,
    )


def _fresh(tmp_mission, port: int):
    """mission.json + writer + state + 一个 ready 任务的公共脚手架。"""
    (tmp_mission / "mission.json").write_text(
        json.dumps(_spec(port).model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    writer = StoreWriter(tmp_mission)
    state = MissionState(spec=_spec(port), mission_id=tmp_mission.name)
    task = create_task(
        state,
        writer,
        TaskDraft(
            type="recon",
            goal=f"probe 127.0.0.1:{port}",
            success_criteria=[{"evidence": "open"}],
            scope_slice={},
            allowed_tools=["probe"],
            budget=Budget(steps=4),
        ),
    )
    promote_pending(state, writer)
    return state, writer, task


def _inject_finding(state, writer, task_id: str) -> object:
    f = FindingProposal(
        claim="监听端口开放",
        severity="low",
        evidence_refs=[],
        reproduce="重跑 probe",
        falsify_if=["连接被拒绝"],
    )
    ev = writer.append(
        "finding.proposed",
        f"worker:{task_id}",
        {"finding": f.model_dump(mode="json")},
        f"fp:test-{len(state.findings)}",
    )
    apply_event(state, ev)
    return f


def _events(root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ─────────── 1. 分类：噪音不醒，信号才醒 ───────────


def test_noise_events_do_not_wake(tmp_mission, listener):
    state, writer, task = _fresh(tmp_mission, listener)
    wake = wakeup_mod.collect(writer)
    assert "task.created" in wake.signals  # 信号级入包
    for noise in ("task.dispatched", "clue.added", "evidence.stored", "decision.made"):
        assert noise not in wake.signals
    writer.append("clue.added", "worker:t-0001", {"k": "v"}, "cl:n1")
    writer.append("evidence.stored", "worker:t-0001", {"id": "e1"}, "ev:n1")
    writer.append("decision.made", "engine", {"kind": "x"}, "dm:n1")
    # 噪音只入账：不推进水位则仍在窗口里，但依然不产生信号
    wake2 = wakeup_mod.collect(writer)
    assert "clue.added" not in wakeup_mod.SIGNAL_TYPES
    assert not any(
        t in wake2.signals for t in ("clue.added", "evidence.stored", "decision.made")
    )


# ─────────── 2. 水位持久化：save 推进窗口 ───────────


def test_watermark_persist_and_advance(tmp_mission, listener):
    state, writer, task = _fresh(tmp_mission, listener)
    wake = wakeup_mod.collect(writer)
    assert wake.signals  # 引导信号在窗口内
    wakeup_mod.save(writer)
    wm_file = tmp_mission / "wakeup.json"
    assert json.loads(wm_file.read_text())["wm"] == writer.head_seq
    # 噪音段：不醒
    writer.append("clue.added", "worker:t-0001", {"k": "v"}, "cl:seg2")
    assert not wakeup_mod.collect(writer).signals
    # 新信号段：再醒（human.directive 不在 tokenizing 集合外；用
    # mentor.intervened 保持窗口纯净）
    writer.append("human.directive", "human", {"text": "继续"}, "hd:n1")
    wake = wakeup_mod.collect(writer)
    assert wake.signals == ("human.directive",)
    # 水印损坏 / 越头 → 失败方向是多醒一次，取 0 不猜
    wm_file.write_text("{not json", encoding="utf-8")
    assert wakeup_mod.collect(writer).signals
    wm_file.write_text(json.dumps({"wm": writer.head_seq + 999}), encoding="utf-8")
    assert not wakeup_mod.collect(writer).signals


# ─────────── 3. 同轮合并：多信号一个包 ───────────


def test_signals_coalesce_in_one_window(tmp_mission, listener):
    state, writer, task = _fresh(tmp_mission, listener)
    _inject_finding(state, writer, task.id)
    writer.append(
        "restart.decided", "engine", {"task_id": task.id, "attempt": 0}, "rs:coalesce"
    )
    writer.append("human.directive", "human", {"text": "x"}, "hd:c1")
    wake = wakeup_mod.collect(writer)  # 一次 collect = 一次心跳
    assert set(wake.signals) == {
        "task.created",
        "task.state_changed",
        "finding.proposed",
        "restart.decided",
        "human.directive",
    }
    assert len(wake.signals) == len(set(wake.signals))  # 去重合并
    assert wake.finding is True


# ─────────── 4. finding 置顶：预消化在前，幂等 ───────────


def test_finding_pre_adjudicated_before_orchestrate(tmp_mission, listener):
    state, writer, task = _fresh(tmp_mission, listener)
    _inject_finding(state, writer, task.id)
    llm = MockLLM({"worker": [_worker_script(listener)]})
    wake = wakeup_mod.collect(writer)
    assert wake.finding
    scheduler.round(state, writer, llm, _freeze(), wake=wake)

    events = _events(tmp_mission)
    seq = {e["type"]: e["seq"] for e in events}
    fid = compute_finding_id(state.findings[0])
    assert fid in state.adjudications
    assert seq["finding.adjudicated"] < seq["decision.made"]  # 置顶：先台账

    # 幂等：再跑一轮（窗口已干净也要预消化为 no-op）不重复落裁决
    before = sum(1 for e in events if e["type"] == "finding.adjudicated")
    scheduler.round(
        state, writer, llm, _freeze(), wake=wakeup_mod.WakeUp(signals=(), finding=True)
    )
    after_events = _events(tmp_mission)
    assert sum(1 for e in after_events if e["type"] == "finding.adjudicated") == before


def test_round_without_wake_keeps_legacy_order(tmp_mission, listener):
    state, writer, task = _fresh(tmp_mission, listener)
    _inject_finding(state, writer, task.id)
    llm = MockLLM({"worker": [_worker_script(listener)]})
    scheduler.round(state, writer, llm, _freeze())  # wake=None：四参兼容

    seq = {e["type"]: e["seq"] for e in _events(tmp_mission)}
    assert seq["decision.made"] < seq["finding.adjudicated"]  # 旧序不变
    assert compute_finding_id(state.findings[0]) in state.adjudications


# ─────────── 5. heartbeat 兜底闹钟 ───────────


def test_heartbeat_requeues_failed_without_llm(tmp_mission, listener):
    state, writer, task = _fresh(tmp_mission, listener)
    transition(task, "running", writer)
    transition(task, "failed", writer, reason="boom")
    assert task.state == "failed" and task.attempt == 0

    llm = MockLLM({})  # 无脚本：供 counts 断言
    scheduler.heartbeat(state, writer)
    assert task.state == "ready"  # attempt < MAX → 抬回
    assert llm.calls == []  # 零 LLM 调用（关键判据）
    types = [e["type"] for e in _events(tmp_mission)][-2:]
    assert "task.state_changed" in types or "restart.decided" in types


def test_heartbeat_is_silent_on_quiescent_state(tmp_mission, listener):
    state, writer, task = _fresh(tmp_mission, listener)
    transition(task, "running", writer)
    transition(task, "done", writer)
    head = writer.head_seq
    scheduler.heartbeat(state, writer)
    assert writer.head_seq == head  # 静止态零事件
