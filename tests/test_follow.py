"""Part 3（watch / --follow）验收测试。

- render_line 逐字节确定；
- watch 全程只读：前后 sha256 与行数不变；
- 追加事件 1s 内渲染新行；
- 已结束 mission 整段渲染并以终止原因对应退出码退出。
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.infra.llm import MockLLM
from pa.models import Event, MissionSpec, now
from pa.report import follow
from pa.runtime import run
from pa.store.writer import StoreWriter

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def listener() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


def _freeze():
    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])


def _spec(port: int) -> MissionSpec:
    from pa.models import AcceptanceItem, Budget

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


def test_render_line_byte_deterministic():
    ev = _ev(
        7,
        "clue.added",
        {
            "clue": {
                "kind": "port",
                "key": "10.0.0.5:80",
                "value": "open",
                "confidence": 0.9,
                "evidence_id": None,
            },
            "task_id": "t-0001",
        },
    )
    a = follow.render_line(ev)
    b = follow.render_line(ev)
    assert a == b, "同一事件渲染两次必须逐字节相等"
    assert "step 000" in a and "#7" in a and "port" in a
    # step / dur 参数也要确定性
    assert follow.render_line(ev, step=3, dur_ms=12) == follow.render_line(
        ev, step=3, dur_ms=12
    )


def _run_finished_mission(d: Path, listener: int):
    probe = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "open",
        },
        ensure_ascii=False,
    )
    planner = json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{listener}",
                    "success_criteria": [{"evidence": "open"}],
                    "scope_slice": {"host": "127.0.0.1"},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {},
            "rationale": "r",
        },
        ensure_ascii=False,
    )
    llm = MockLLM({"planner": [planner], "worker": [probe]})
    return run(d, _spec(listener), llm=llm, assets=_freeze())


def test_watch_readonly_and_finished_exit_code(tmp_mission, listener, monkeypatch):
    monkeypatch.setenv("SECPT_MENTOR_MODE", "off")
    _run_finished_mission(tmp_mission, listener)
    ledger = tmp_mission / "events.jsonl"
    before_hash = hashlib.sha256(ledger.read_bytes()).hexdigest()
    before_lines = len(ledger.read_text(encoding="utf-8").splitlines())

    out = io.StringIO()
    rc = follow.watch(tmp_mission, out=out, poll=0.05)
    assert rc == 0, "completed → 0"
    assert "completed" in out.getvalue()
    assert "report.md" in out.getvalue()

    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before_hash, (
        "watch 不得改动账本"
    )
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == before_lines


def test_watch_renders_appended_events_within_1s(tmp_mission):
    writer = StoreWriter(tmp_mission)
    (tmp_mission / "mission.json").write_text(
        _spec(1).model_dump_json(), encoding="utf-8"
    )
    writer.append(
        "mission.created",
        "engine",
        {"objective": "o", "targets": ["127.0.0.1"]},
        "mc:m",
    )
    out = io.StringIO()
    stop = threading.Event()
    holder: dict = {}

    def _w():
        holder["rc"] = follow.watch(tmp_mission, out=out, poll=0.05, stop=stop)

    t = threading.Thread(target=_w, daemon=True)
    t.start()
    time.sleep(0.3)
    writer.append(
        "clue.added",
        "core",
        {
            "clue": {
                "kind": "port",
                "key": "127.0.0.1:1",
                "value": "open",
                "confidence": 0.9,
                "evidence_id": None,
            }
        },
        "cl:1",
    )
    deadline = time.monotonic() + 1.0
    while "127.0.0.1:1" not in out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "127.0.0.1:1" in out.getvalue(), "追加事件 1s 内必须渲染"
    writer.append(
        "mission.finished",
        "engine",
        {"summary": "s", "reason": "completed", "satisfied": []},
        "mf:m",
    )
    t.join(timeout=2)
    assert not t.is_alive()
    assert holder["rc"] == 0


def test_watch_verbose_streams_full_details(tmp_mission, listener, monkeypatch):
    """verbose 流（secpt do 默认口径）：每事件渲染全 payload 细节——决策
    expect、证据 stdout 全文预览，而不是单行摘要；且全程只读。"""
    monkeypatch.setenv("SECPT_MENTOR_MODE", "off")
    _run_finished_mission(tmp_mission, listener)
    ledger = tmp_mission / "events.jsonl"
    before_hash = hashlib.sha256(ledger.read_bytes()).hexdigest()

    out = io.StringIO()
    rc = follow.watch(tmp_mission, out=out, poll=0.05, verbose=True)
    assert rc == 0
    text = out.getvalue()
    assert "[action.started]" in text, "详情块头部应带类型/actor/ts"
    assert "\nexpect:\n" in text, "worker 决策的 expect 应全量渲染"
    assert "── 证据全文 ev-" in text, "证据事件应附全文预览"
    assert "[mission.finished]" in text and "report.md" in text
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before_hash, (
        "verbose watch 也不得改动账本"
    )


def test_watch_plain_mode_has_no_full_details(tmp_mission, listener, monkeypatch):
    """单行摘要口径（run --follow / watch）：不该倒出全 payload 细节。"""
    monkeypatch.setenv("SECPT_MENTOR_MODE", "off")
    _run_finished_mission(tmp_mission, listener)
    out = io.StringIO()
    assert follow.watch(tmp_mission, out=out, poll=0.05) == 0
    assert "\nexpect:\n" not in out.getvalue()


def test_watch_plateau_exit_code_matches_run(tmp_mission, listener, monkeypatch):
    monkeypatch.setenv("SECPT_MENTOR_MODE", "off")
    bad = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "10.255.255.1", "port": 80},
            "expect": "x",
        },
        ensure_ascii=False,
    )
    planner = json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{listener}",
                    "success_criteria": [{"evidence": "never-ever-met"}],
                    "scope_slice": {"host": "127.0.0.1"},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {},
            "rationale": "r",
        },
        ensure_ascii=False,
    )
    llm = MockLLM({"planner": [planner], "worker": [bad]})
    state = run(tmp_mission, _spec(listener), llm=llm, assets=_freeze())
    assert state.finish_reason == "plateau"
    rc = follow.watch(tmp_mission, out=io.StringIO(), poll=0.05)
    assert rc == 3, "plateau → 3，与 run 退出码一致"
