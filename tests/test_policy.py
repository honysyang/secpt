"""S12 验收：教练三档模式（off / shadow / enforce）与阈值生效。

- shadow 跑完整 mission：账本有 mentor.advisory，任务状态与 off 逐字节相同；
- off：零 mentor 事件；
- enforce 下 thrashing 触发 supersede，事件可重放（restore 后仍 superseded）；
- 计划缺 criteria：shadow 只记 advisory（任务照建），enforce 才拦（不建）；
- 改 thresholds.py 的值，advisory 触发次数随之变化。
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.infra.llm import MockLLM
from pa.models import AcceptanceItem, Budget, MissionSpec
from pa.policy import thresholds
from pa.runtime import run
from pa.store.view import StoreView

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
    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts",
                                [ROOT / "skills"])


def _spec(port: int) -> MissionSpec:
    return MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[AcceptanceItem(id="acc-1", item="端口开放被证实",
                                   evidence="never-ever-met")],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{port}"],
    )


def _planner(port: int, *, criteria=None, scope=None) -> str:
    return json.dumps(
        {"tasks": [{"type": "recon",
                    "goal": f"probe 127.0.0.1:{port}",
                    "success_criteria": criteria
                    if criteria is not None else [{"evidence": "never-ever-met"}],
                    "scope_slice": scope if scope is not None else {"host": "127.0.0.1"},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4}}],
         "current_strategy": {}, "rationale": "r"}, ensure_ascii=False)


FINISH = json.dumps({"kind": "finish", "summary": "声称完成"},
                    ensure_ascii=False)


def _events(d):
    return [json.loads(l) for l in
            (d / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _dump(state):
    d = state.model_dump(mode="json")
    d.pop("spent_seconds")
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


# ─────────────────────────── off / shadow ───────────────────────────

def test_off_mode_zero_mentor_events(tmp_mission, listener, monkeypatch):
    monkeypatch.setenv("PA_MENTOR_MODE", "off")
    d = tmp_mission / "m"
    llm = MockLLM({"planner": [_planner(listener)], "worker": [FINISH]})
    run(d, _spec(listener), llm=llm, assets=_freeze())
    assert not [e for e in _events(d)
                if e["type"].startswith(("mentor.advisory", "mentor.intervened"))]


def test_shadow_byte_identical_to_off_with_advisories(tmp_mission, listener,
                                                      monkeypatch):
    # 同一 mission 两种模式各跑一遍（同名子目录，mission_id 一致才可比对）
    monkeypatch.setenv("PA_MENTOR_MODE", "off")
    d_off = tmp_mission / "off" / "m"
    llm = MockLLM({"planner": [_planner(listener)], "worker": [FINISH]})
    s_off = run(d_off, _spec(listener), llm=llm, assets=_freeze())

    monkeypatch.setenv("PA_MENTOR_MODE", "shadow")
    d_sh = tmp_mission / "shadow" / "m"
    llm = MockLLM({"planner": [_planner(listener)], "worker": [FINISH]})
    s_sh = run(d_sh, _spec(listener), llm=llm, assets=_freeze())

    assert _dump(s_off) == _dump(s_sh)          # 任务状态逐字节一致
    adv = [e for e in _events(d_sh) if e["type"] == "mentor.advisory"]
    assert adv, "shadow 模式应有 advisory"
    # 约束 C：payload 七字段齐全
    for e in adv:
        assert {"level", "issue", "metric", "threshold", "would_do",
                "target", "converged_after"} <= set(e["payload"])
    assert not [e for e in _events(d_sh) if e["type"] == "mentor.intervened"]


# ─────────────────────────── enforce ───────────────────────────

def test_enforce_thrashing_supersede_replayable(tmp_mission, listener,
                                                monkeypatch):
    monkeypatch.setenv("PA_MENTOR_MODE", "enforce")
    d = tmp_mission / "m"
    llm = MockLLM({"planner": [_planner(listener)], "worker": [FINISH]})
    state = run(d, _spec(listener), llm=llm, assets=_freeze())

    task = state.tasks["t-0001"]
    assert task.state == "superseded"           # thrashing 动作生效
    evs = _events(d)
    sup = [e for e in evs if e["type"] == "mentor.intervened"
           and e["payload"].get("detail", {}).get("action") == "supersede"]
    assert sup, "enforce 应落 supersede 的 mentor.intervened"
    # 事件可重放：纯重放后任务仍 superseded
    restored = StoreView(d).restore()
    assert restored.tasks["t-0001"].state == "superseded"
    # vs_rate 信号同时触发降级动作
    assert any(e["type"] == "model.switched" for e in evs)


def test_plan_missing_criteria_shadow_advises_enforce_blocks(tmp_mission,
                                                            listener,
                                                            monkeypatch):
    bad_plan = _planner(listener, criteria=[], scope={"host": "127.0.0.1"})

    monkeypatch.setenv("PA_MENTOR_MODE", "shadow")
    d_sh = tmp_mission / "sh" / "m"
    llm = MockLLM({"planner": [bad_plan], "worker": [FINISH]})
    s_sh = run(d_sh, _spec(listener), llm=llm, assets=_freeze())
    assert "t-0001" in s_sh.tasks                 # shadow 不拦，任务照建
    adv = [e for e in _events(d_sh) if e["type"] == "mentor.advisory"]
    assert any(e["payload"]["issue"] == "no_criteria" for e in adv)

    monkeypatch.setenv("PA_MENTOR_MODE", "enforce")
    d_en = tmp_mission / "en" / "m"
    llm = MockLLM({"planner": [bad_plan], "worker": [FINISH]})
    s_en = run(d_en, _spec(listener), llm=llm, assets=_freeze())
    assert not s_en.tasks, "enforce 应拦掉缺判据的草案"
    adv = [e for e in _events(d_en) if e["type"] == "mentor.advisory"]
    assert any(e["payload"]["issue"] == "no_criteria"
               and e["payload"]["would_do"] == "block_task" for e in adv)


# ─────────────────────────── 阈值生效 ───────────────────────────

def test_threshold_change_changes_advisory_count(tmp_mission, listener,
                                                 monkeypatch):
    monkeypatch.setenv("PA_MENTOR_MODE", "shadow")
    # 默认阈值：thrashing 信号触发（任务打满 3 次尝试）
    d1 = tmp_mission / "a" / "m"
    llm = MockLLM({"planner": [_planner(listener)], "worker": [FINISH]})
    run(d1, _spec(listener), llm=llm, assets=_freeze())
    n_default = sum(1 for e in _events(d1)
                    if e["type"] == "mentor.advisory"
                    and e["payload"]["issue"] == "thrashing")
    assert n_default >= 1

    # 阈值抬高到 99：同一 mission 不再触发
    monkeypatch.setitem(thresholds.THRESHOLDS, "thrashing_attempts", 99)
    d2 = tmp_mission / "b" / "m"
    llm = MockLLM({"planner": [_planner(listener)], "worker": [FINISH]})
    run(d2, _spec(listener), llm=llm, assets=_freeze())
    n_raised = sum(1 for e in _events(d2)
                   if e["type"] == "mentor.advisory"
                   and e["payload"]["issue"] == "thrashing")
    assert n_raised == 0
