"""S9 验收：worker 有界 ReAct + reflect 裁决（§7.5.1 四条硬约束 + 追加验收）。

追加验收：
- worker 跑 3 次工具后，发给模型的 user 含非空 <untrusted> 块；
- 步数耗尽返回 partial（不是 done）；
- worker 报 done 但判据未满足 → judge 返回 virtual_success；
- blocked 之后 REGISTRY 调用计数 == 1（不再发起工具调用）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa.assets import AssetSnapshot
from pa.core.reflect import classify_blocked, judge, validate_evidence
from pa.core.worker import UNTRUSTED_RING, run_worker
from pa.infra import evidence
from pa.infra.executor import ProcResult
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem, Budget, Clue, Criterion, Failure, MissionSpec,
    MissionState, Task, WorkerResult,
)
from pa.store.view import StoreView
from pa.store.writer import StoreWriter
from pa.tools import REGISTRY, ToolSpec

_REPO = Path(__file__).resolve().parent.parent

PROBE_OPEN = json.dumps({
    "kind": "call_tool", "tool": "probe",
    "args": {"target": "10.0.0.5", "port": 80},
    "expect": "看到 open"}, ensure_ascii=False)
FINISH = json.dumps({"kind": "finish", "summary": "完成",
                     "satisfied": ["a1"]}, ensure_ascii=False)


@pytest.fixture
def fake_probe():
    """把 REGISTRY 的 probe 换成无网络桩（返回 open），测试结束还原。
    真实 probe 会对 10.0.0.5 发起 TCP 连接，无外网环境会挂到超时。"""
    original = REGISTRY["probe"]

    def fn(args, ctx):
        return ProcResult(0, b"open\n", b"")

    REGISTRY["probe"] = ToolSpec(name="probe", risk_tier="low",
                                 side_effect="read", requires=[], schema={},
                                 produces_evidence=True, fn=fn)
    yield
    REGISTRY["probe"] = original


def _setup(tmp_mission, steps=12):
    spec = MissionSpec(
        objective="o", targets=["10.0.0.5:80"],
        acceptance=[AcceptanceItem(id="a1", item="端口清单",
                                   evidence="port.open")],
        budget=Budget(), allowed_scope=["10.0.0.0/24:80"])
    (tmp_mission / "mission.json").write_text(spec.model_dump_json(),
                                              encoding="utf-8")
    state = StoreView(tmp_mission).restore()
    writer = StoreWriter(tmp_mission)
    assets = AssetSnapshot.freeze(_REPO / "pa/core/prompts", [_REPO / "skills"])
    task = Task(id="t-0001", mission_id=tmp_mission.name, type="recon",
                goal="探活", success_criteria=[Criterion(evidence="open")],
                scope_slice={}, allowed_tools=["probe"],
                budget=Budget(steps=steps))
    return state, writer, assets, task


def test_untrusted_accumulates_after_3_tool_runs(tmp_mission, fake_probe):
    """追加验收一：3 次工具后，发给模型的 user 含非空 <untrusted> 块。"""
    state, writer, assets, task = _setup(tmp_mission)
    task.success_criteria = [Criterion(evidence="port.open")]   # 永不满足
    script = {"worker": [PROBE_OPEN] * (UNTRUSTED_RING + 1)}
    llm = MockLLM(script)
    run_worker(task, state, writer, llm, assets)
    assert len(llm.calls) >= 4
    last_user = llm.calls[-1]["user"]
    assert "<untrusted>" in last_user and "</untrusted>" in last_user
    assert "（暂无工具产出）" not in last_user.split("<untrusted>")[1]
    assert "exit=0" in last_user                     # 工具摘要确实进去了
    # 环形缓冲：只有最近 3 段（4 次调用，第 1 段已滚出）
    assert last_user.count("exit=0") == UNTRUSTED_RING


def test_steps_exhausted_returns_partial(tmp_mission, fake_probe):
    """追加验收二：步数耗尽（模型一直不 finish）返回 partial，不是 done。"""
    state, writer, assets, task = _setup(tmp_mission, steps=3)
    task.success_criteria = [Criterion(evidence="port.open")]   # 永不满足
    llm = MockLLM({"worker": [PROBE_OPEN] * 10})     # 永远 call_tool
    res = run_worker(task, state, writer, llm, assets)
    assert res.status == "partial" and res.cost["steps"] == 3


def test_judge_virtual_success(tmp_mission, fake_probe):
    """追加验收三：报 done 但判据未满足 → judge 返回 virtual_success。"""
    state, writer, assets, task = _setup(tmp_mission)
    llm = MockLLM({"worker": [FINISH]})
    res = run_worker(task, state, writer, llm, assets)
    assert res.status == "done"                      # 只是主张
    assert judge(task, res, state) == "virtual_success"
    # 补上线索后同一主张变成 success
    ev = writer.append("clue.added", "worker:t-0001",
                       {"clue": Clue(kind="port", key="port.open",
                                     value="open").model_dump(mode="json")},
                       "clue:x:port.open")
    from pa.store.event import apply_event
    apply_event(state, ev)
    assert judge(task, res, state) == "success"


def test_blocked_returns_immediately_no_more_tool_calls(tmp_mission):
    """追加验收四：blocked 之后 REGISTRY 调用计数 == 1。"""
    state, writer, assets, task = _setup(tmp_mission)
    task.success_criteria = [Criterion(evidence="port.open")]   # 走到第二次调用
    calls: list[str] = []

    def counting_probe(args, ctx):
        calls.append(args["target"])
        # 第一次执行后缩范围：让下一次 call_tool 走 out_of_scope 路径
        state.spec.allowed_scope = ["10.0.0.0/24:443"]
        return ProcResult(0, b"open\n", b"")

    original = REGISTRY["probe"]
    REGISTRY["probe"] = ToolSpec(
        name="probe", risk_tier="low", side_effect="read", requires=[],
        schema={}, produces_evidence=True, fn=counting_probe)
    try:
        bad = json.dumps({"kind": "call_tool", "tool": "probe",
                          "args": {"target": "10.0.0.5", "port": 80},
                          "expect": "x"}, ensure_ascii=False)
        llm = MockLLM({"worker": [PROBE_OPEN, bad, PROBE_OPEN]})
        res = run_worker(task, state, writer, llm, assets)
        assert res.status == "blocked"
        assert res.failure.cls == "AuthZ" and res.failure.detail == "out_of_scope"
        assert len(calls) == 1                # 只执行了第 1 次，blocked 后立即返回
    finally:
        REGISTRY["probe"] = original


def test_tried_carries_over_across_attempts(tmp_mission, fake_probe):
    """约束 B：返回的 tried 含 task.tried，供调度器并回。"""
    state, writer, assets, task = _setup(tmp_mission)
    task.tried = ["fp-old"]
    llm = MockLLM({"worker": [PROBE_OPEN, FINISH]})
    res = run_worker(task, state, writer, llm, assets)
    assert res.tried[0] == "fp-old" and len(res.tried) == 2


def test_clues_land_in_ledger_and_criteria_trigger_done(tmp_mission, fake_probe):
    """跑通路径：probe open → 线索落账（幂等可重放）→ 判据满足 → done。"""
    state, writer, assets, task = _setup(tmp_mission)
    llm = MockLLM({"worker": [PROBE_OPEN]})
    res = run_worker(task, state, writer, llm, assets)
    assert res.status == "done" and judge(task, res, state) == "success"
    assert res.evidence_refs and res.clues
    restored = StoreView(tmp_mission).restore()
    assert restored.clue_seq == len(restored.clues) > 0
    keys = [c.key for c in restored.clues]
    assert "10.0.0.5:80" in keys


def test_classify_blocked_mapping():
    """S10 裁定映射：AuthZ 只含真正的越界/需审批；
    tool_not_allowed → Plan（任务定义错）；tool_not_found → Model（幻觉）。"""
    assert classify_blocked("missing_target") == "Tool"
    assert classify_blocked("out_of_scope") == "AuthZ"
    assert classify_blocked("needs_approval") == "AuthZ"
    assert classify_blocked("tool_not_allowed") == "Plan"
    assert classify_blocked("tool_not_found") == "Model"
    assert classify_blocked("rate_limited") == "Env"
    from pa.errors import SecptError
    with pytest.raises(SecptError):
        classify_blocked("budget_exceeded")   # 不是失败：worker 返回 partial


def test_validate_evidence_via_view(tmp_mission):
    """裁定 3 签名：validate_evidence(view, refs, task_id=None)。"""
    state, writer, assets, task = _setup(tmp_mission)
    eid = evidence.put(tmp_mission, b'{"tool": "probe", "exit": 0}',
                       task_id="t-0001")
    view = StoreView(tmp_mission)
    assert validate_evidence(view, [eid], task_id="t-0001") == []
    assert validate_evidence(view, ["ev-" + "0" * 16]) == ["ev-" + "0" * 16]
    assert validate_evidence(view, [eid], task_id="t-9999") == [eid]
