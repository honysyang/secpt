"""S6 网关验收：越界零证据、缓存不重复执行、失败重试封顶、工具子集、
范围先于去重（S6 验收六条，全部专测）。"""
from __future__ import annotations

import json

import pytest

from pa.gateway import Outcome, execute
from pa.gateway.execute import BLOCKED_REASONS
from pa.infra.executor import ProcResult
from pa.models import AcceptanceItem, Budget, MissionSpec
from pa.store.view import StoreView
from pa.store.writer import StoreWriter
from pa.tools import REGISTRY, ToolSpec


def _write_mission(root, scope=("10.0.0.0/24:80",)) -> None:
    spec = MissionSpec(
        objective="评估暴露面", targets=["10.0.0.5:80"],
        acceptance=[AcceptanceItem(id="a1", item="端口清单", evidence="port.open")],
        budget=Budget(), allowed_scope=list(scope),
    )
    (root / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")


def _sw(root) -> tuple:
    return StoreView(root).restore(), StoreWriter(root)


def _events(root):
    return [json.loads(x) for x in
            (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def mark_tool(tmp_mission):
    """假工具：每次调用写一次 marker（证明"执行了"），返回 exit 0。"""
    calls: list[dict] = []

    def fn(args: dict, ctx) -> ProcResult:
        calls.append(dict(args))
        (tmp_mission / args["marker"]).write_text("ran", encoding="utf-8")
        return ProcResult(0, b"ok\n", b"")

    REGISTRY["mark"] = ToolSpec(name="mark", risk_tier="low",
                                side_effect="write", requires=[], schema={},
                                produces_evidence=True, fn=fn)
    yield calls
    del REGISTRY["mark"]


def test_out_of_scope_blocked_no_evidence(tmp_mission):
    """验收一：越界目标零证据、有且仅有一条 action.blocked。"""
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    out = execute(state, w, tool_name="probe",
                  args={"target": "192.168.1.1", "port": 80}, task_id="t1")
    assert isinstance(out, Outcome)
    assert out.ok is False and out.blocked_reason == "out_of_scope"
    assert not (tmp_mission / "evidence").exists()        # 零新增证据文件
    blocked = [e for e in _events(tmp_mission) if e["type"] == "action.blocked"]
    assert len(blocked) == 1                              # 有且仅有一条
    assert blocked[0]["payload"]["reason"] == "out_of_scope"


def test_cache_hit_no_reexecute(tmp_mission, mark_tool):
    """验收二：第二次 cached=True，且不再执行（marker 只写一次）。"""
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    args = {"target": "10.0.0.5", "port": 80, "marker": "m1"}
    out1 = execute(state, w, tool_name="mark", args=args, task_id="t1")
    assert out1.cached is False and out1.evidence_id
    out2 = execute(state, w, tool_name="mark", args=args, task_id="t1")
    assert out2.cached is True and out2.evidence_id == out1.evidence_id
    assert len(mark_tool) == 1                            # 没有第二次执行
    finished = [e for e in _events(tmp_mission)
                if e["type"] == "action.finished"]
    assert len(finished) == 2 and finished[-1]["payload"]["cached"] is True


def test_failure_retry_then_exhausted(tmp_mission):
    """验收三：失败 2 次后第 3 次 cached + cache_hit_exhausted，不再执行。"""
    calls: list[dict] = []

    def fn(args: dict, ctx) -> ProcResult:
        calls.append(dict(args))
        return ProcResult(1, b"", b"boom")

    REGISTRY["flaky"] = ToolSpec(name="flaky", risk_tier="low",
                                 side_effect="read", requires=[], schema={},
                                 produces_evidence=True, fn=fn)
    try:
        _write_mission(tmp_mission)
        state, w = _sw(tmp_mission)
        args = {"target": "10.0.0.5", "port": 80}
        o1 = execute(state, w, tool_name="flaky", args=args, task_id="t1")
        assert o1.cached is False and o1.exit_code == 1
        o2 = execute(state, w, tool_name="flaky", args=args, task_id="t1")
        assert o2.cached is False                         # 未满 2 次，允许重试
        o3 = execute(state, w, tool_name="flaky", args=args, task_id="t1")
        assert o3.cached is True and o3.cache_hit_exhausted is True
        assert o3.exit_code == 1 and o3.stderr == "boom"
        assert len(calls) == 2                            # 第 3 次没有执行
        # 重启一致性：fp_attempts 从账本重放，仍是 2
        state2 = StoreView(tmp_mission).restore()
        fp = next(iter(state2.meta["fp_attempts"]))
        assert state2.meta["fp_attempts"][fp] == 2
        # 命中审计链：两条非 cached fin + 一条 cached fin
        finished = [e for e in _events(tmp_mission)
                    if e["type"] == "action.finished"]
        assert sum(1 for e in finished if not e["payload"].get("cached")) == 2
        assert sum(1 for e in finished if e["payload"].get("cached")) == 1
    finally:
        del REGISTRY["flaky"]


def test_tool_not_allowed(tmp_mission):
    """验收四：不在 allowed_tools 的工具不执行、不落证据、有审计事件。"""
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    out = execute(state, w, tool_name="probe",
                  args={"target": "10.0.0.5", "port": 80}, task_id="t1",
                  allowed_tools=["shell"])
    assert out.ok is False and out.blocked_reason == "tool_not_allowed"
    assert not (tmp_mission / "evidence").exists()
    blocked = [e for e in _events(tmp_mission) if e["type"] == "action.blocked"]
    assert len(blocked) == 1 and blocked[0]["payload"]["reason"] == "tool_not_allowed"


def test_tool_not_found_is_enum_value(tmp_mission):
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    out = execute(state, w, tool_name="nope", args={}, task_id="t1")
    assert out.blocked_reason == "tool_not_found"


def test_scope_checked_before_dedupe(tmp_mission, mark_tool):
    """验收六：先缓存成功、再缩范围，命中时仍必须 out_of_scope。"""
    _write_mission(tmp_mission, scope=("10.0.0.0/24:80",))
    state, w = _sw(tmp_mission)
    args = {"target": "10.0.0.5", "port": 80, "marker": "m2"}
    out1 = execute(state, w, tool_name="mark", args=args, task_id="t1",
                   allowed_tools=["mark"])
    assert out1.cached is False and out1.ok
    state.spec.allowed_scope = ["10.0.0.0/24:443"]        # 缩范围：80 不再在界内
    out2 = execute(state, w, tool_name="mark", args=args, task_id="t1",
                   allowed_tools=["mark"])
    assert out2.ok is False and out2.blocked_reason == "out_of_scope"
    assert out2.cached is False                           # 缓存不得越过 scope


def test_semantic_args_in_evidence_meta(tmp_mission, mark_tool):
    """裁定 6：content.args 完整，meta.semantic_args 剔除 volatile。"""
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    args = {"target": "10.0.0.5", "port": 80, "timeout": 60,
            "retries": 3, "marker": "m3"}
    out = execute(state, w, tool_name="mark", args=args, task_id="t1")
    content = StoreView(tmp_mission).evidence(out.evidence_id)
    assert content["args"]["timeout"] == 60               # 完整 args
    meta = StoreView(tmp_mission).evidence_meta(out.evidence_id)
    assert "timeout" not in meta["semantic_args"]         # 与指纹同口径
    assert meta["semantic_args"]["port"] == 80


def test_blocked_reasons_closed_enum():
    """裁定 5+7：封闭枚举七值，tool_not_found 是文档 unknown_tool 的别名。"""
    assert set(BLOCKED_REASONS) == {
        "tool_not_found", "tool_not_allowed", "missing_target",
        "out_of_scope", "needs_approval", "rate_limited", "budget_exceeded"}


def test_missing_target_blocked(tmp_mission):
    """裁定 2：网络工具缺 target → missing_target（封闭枚举值）。"""
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    out = execute(state, w, tool_name="probe", args={}, task_id="t1")
    assert out.ok is False and out.blocked_reason == "missing_target"
    assert not (tmp_mission / "evidence").exists()


def test_fs_declares_no_network(tmp_mission):
    """裁定 2：fs 显式 requires_network=False，不经范围检查也能执行。"""
    _write_mission(tmp_mission, scope=())              # 空 allowed 也不该拦 fs
    (tmp_mission / "workspace").mkdir(exist_ok=True)
    (tmp_mission / "workspace" / "n.txt").write_text("x", encoding="utf-8")
    state, w = _sw(tmp_mission)
    out = execute(state, w, tool_name="fs",
                  args={"op": "read", "path": "n.txt"}, task_id="t1")
    assert out.ok is True and out.stdout == "x" and out.evidence_id
