"""Part 1 五处修正的验收测试。

1.1 completed 只看验收达成：5 任务中 1 个满足验收 → 该轮即 completed，
    后续任务零派发（worker 调用次数 == 1）；
1.2 退出码 4 命名 halted：四种终态各跑一次断言退出码；
1.3 derive_stats 移进 scheduler.round：本轮 worker 失败当轮进 advisory.metric；
1.4 advisory occurrences：10 轮 thrashing → 事件数 1、occurrences==10、重放一致；
"""

from __future__ import annotations

import json
import socket
from datetime import timedelta
from pathlib import Path

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.infra.llm import MockLLM
from pa.models import AcceptanceItem, Budget, MissionSpec, MissionState, Task, now
from pa.policy import coach
from pa.runtime import run
from pa.store.derive import advisory_occurrences, derive_stats
from pa.store.view import StoreView
from pa.store.writer import StoreWriter
from pa.cli import main as cli_main

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


def _spec(port: int, **kw) -> MissionSpec:
    return MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1",
                item="端口开放被证实",
                evidence=kw.get("evidence", "open"),
                target="127.0.0.1",
            )
        ],
        budget=kw.get("budget") or Budget(steps=12, seconds=600, tokens=40000),
        deadline=kw.get("deadline"),
        allowed_scope=[f"127.0.0.1:{port}"],
    )


# ── 1.1 ────────────────────────────────────────────────────────────


def test_completed_drains_queue_first(tmp_mission, listener):
    """收口口径（联调裁定）：验收达成 ≠ 活干完——队列未排空必须继续跑，
    排空后才判 completed（旧口径「验收达成即收口」导致实测 completed
    时还有任务从未派发、findings 0 的浅层完成）。P0-2 之后 deps 一律
    丢弃、任务全部并列：4 个后续任务同样被派发（判据 never-met 不满足
    → virtual_success → failed 重试至耗尽），t-0005 在验收达成后仍被
    派发跑完，全部失败 → 队列排空 → completed。"""
    tasks = [
        {
            "type": "recon",
            "goal": f"probe 127.0.0.1:{listener}",
            "success_criteria": [{"evidence": "open"}],
            "scope_slice": {"host": "127.0.0.1"},
            "allowed_tools": ["probe"],
            "budget": {"steps": 4},
        }
    ]
    for i in range(4):  # t-0002..t-0005 全部依赖 t-0001
        tasks.append(
            {
                "type": "recon",
                "goal": f"后续任务 {i}",
                "success_criteria": [{"evidence": "never-met"}],
                "deps": ["t-0001"],
                "scope_slice": {"host": "127.0.0.1"},
                "allowed_tools": ["probe"],
                "budget": {"steps": 2},
            }
        )
    planner = json.dumps(
        {"tasks": tasks, "current_strategy": {}, "rationale": "r"}, ensure_ascii=False
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "open",
        },
        ensure_ascii=False,
    )
    llm = MockLLM({"planner": [planner], "worker": [worker]})
    state = run(tmp_mission, _spec(listener), llm=llm, assets=_freeze())

    assert state.finish_reason == "completed"
    assert state.tasks["t-0001"].state == "done"
    # P0-2：合法依赖被丢弃，任务并列派发；判据永不满足 → failed 至耗尽。
    # 收口口径：验收达成后 t-0005 照样派发跑完，没有停在 ready
    for tid in ("t-0002", "t-0003", "t-0004", "t-0005"):
        assert state.tasks[tid].state == "failed"
        assert state.tasks[tid].deps == []  # 依赖编排已置空
    # 派发口径看账本（worker 每决策一次调一次 LLM，调用数 ≠ 派发数）
    events = [
        json.loads(line)
        for line in (tmp_mission / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    dispatched = {
        e["payload"]["task_id"] for e in events if e["type"] == "task.dispatched"
    }
    assert dispatched == {"t-0001", "t-0002", "t-0003", "t-0004", "t-0005"}


# ── 1.2 ────────────────────────────────────────────────────────────


def test_exit_codes_four_terminals(tmp_mission, listener, capsys):
    """0 completed / 2 budget / 3 plateau / 4 halted(deadline)。"""
    # completed → 0
    d = tmp_mission / "m0"
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
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "open",
        },
        ensure_ascii=False,
    )
    (d / "spec.json").parent.mkdir(parents=True, exist_ok=True)
    (d / "spec.json").write_text(
        json.dumps(_spec(listener).model_dump(mode="json"), ensure_ascii=False)
    )
    import os

    monkey_env = json.dumps({"planner": [planner], "worker": [worker]})
    os.environ["SECPT_LLM_MOCK"] = monkey_env
    try:
        assert cli_main(["run", str(d / "run"), "--spec", str(d / "spec.json")]) == 0
        # budget → 2（P1-1 后队列排空立即 plateau：预算收口必须用判据永
        # 不满足的任务——失败重排队让队列活不到排空，steps=2 先触顶）
        (d / "spec2.json").write_text(
            json.dumps(
                _spec(
                    listener,
                    evidence="never-met",
                    budget=Budget(steps=2, seconds=600, tokens=40000),
                ).model_dump(mode="json"),
                ensure_ascii=False,
            )
        )
        os.environ["SECPT_LLM_MOCK"] = json.dumps(
            {
                "planner": [
                    json.dumps(
                        {
                            "tasks": [
                                {
                                    "type": "recon",
                                    "goal": f"probe 127.0.0.1:{listener}",
                                    "success_criteria": [{"evidence": "never-met"}],
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
                ],
                "worker": [worker],
            }
        )
        assert cli_main(["run", str(d / "b"), "--spec", str(d / "spec2.json")]) == 2
        # plateau → 3（out_of_scope 首步被拦，队列排空立即收口；用默认
        # 12 步预算的 spec，budget=2 会先收口）
        bad = json.dumps(
            {
                "kind": "call_tool",
                "tool": "probe",
                "args": {"target": "10.255.255.1", "port": 80},
                "expect": "x",
            },
            ensure_ascii=False,
        )
        os.environ["SECPT_LLM_MOCK"] = json.dumps(
            {
                "planner": [
                    json.dumps(
                        {
                            "tasks": [
                                {
                                    "type": "recon",
                                    "goal": f"probe 127.0.0.1:{listener}",
                                    "success_criteria": [{"evidence": "never-met"}],
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
                ],
                "worker": [bad],
            }
        )
        assert cli_main(["run", str(d / "p"), "--spec", str(d / "spec.json")]) == 3
        # halted(deadline) → 4
        past = now() - timedelta(seconds=5)
        (d / "spec4.json").write_text(
            json.dumps(
                _spec(listener, deadline=past).model_dump(mode="json"),
                ensure_ascii=False,
            )
        )
        os.environ["SECPT_LLM_MOCK"] = monkey_env
        assert cli_main(["run", str(d / "h"), "--spec", str(d / "spec4.json")]) == 4
    finally:
        os.environ.pop("SECPT_LLM_MOCK", None)


# ── 1.3 ────────────────────────────────────────────────────────────


def test_advisory_metric_includes_same_round_failure(
    tmp_mission, listener, monkeypatch
):
    """derive_stats 移进 scheduler.round：本轮 worker.returned 的失败当轮
    进入 advisory.metric（数值 +1）。"""
    monkeypatch.setenv("SECPT_MENTOR_MODE", "shadow")
    bad_worker = json.dumps(
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
                    "success_criteria": [{"evidence": "never-met"}],
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
    llm = MockLLM({"planner": [planner], "worker": [bad_worker]})
    run(tmp_mission, _spec(listener), llm=llm, assets=_freeze())

    evs = [
        json.loads(line)
        for line in (tmp_mission / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    wr = [e for e in evs if e["type"] == "worker.returned"]
    adv = [
        e
        for e in evs
        if e["type"] == "mentor.advisory" and e["payload"]["issue"] == "authz_ratio"
    ]
    assert wr and adv
    # advisory 在本轮（worker.returned 之后）落账：metric 含这次 AuthZ 失败
    assert wr[0]["seq"] < adv[0]["seq"]
    assert adv[0]["payload"]["metric"]["failures_by_class"].get("AuthZ") >= 1


# ── 1.4 ────────────────────────────────────────────────────────────


def test_advisory_occurrences_single_event_replayable(tmp_mission, monkeypatch):
    monkeypatch.setenv("SECPT_MENTOR_MODE", "shadow")
    # 直接驱动 coach.on_round ×10（不经完整 run()——完整跑里平台期会在
    # 第 8 步左右提前收口，凑不满 10 轮 thrashing 命中）：
    # 任务 failed 且 attempt=3 ≥ 阈值 → 每轮命中；meta 每轮按 scheduler
    # 的方式由账本重算（derive_stats），advisory_occurrences 口径一致。
    spec = MissionSpec(
        objective="o",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(id="acc-1", item="i", evidence="never", target="127.0.0.1")
        ],
        budget=Budget(steps=100, seconds=6000, tokens=400000),
        allowed_scope=["127.0.0.1"],
    )
    (tmp_mission / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
    state = MissionState(spec=spec, mission_id=tmp_mission.name)
    state.tasks["t-0001"] = Task(
        id="t-0001",
        mission_id=tmp_mission.name,
        type="recon",
        goal="g",
        success_criteria=[],
        scope_slice={},
        allowed_tools=["probe"],
        state="failed",
        attempt=3,
    )
    writer = StoreWriter(tmp_mission)
    view = StoreView(tmp_mission)
    for _ in range(10):
        state.meta.update(derive_stats(view.events(from_seq=1)))
        coach.on_round(state, writer)

    evs = [
        json.loads(line)
        for line in (tmp_mission / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    thr = [
        e
        for e in evs
        if e["type"] == "mentor.advisory" and e["payload"]["issue"] == "thrashing"
    ]
    agg = [
        e
        for e in evs
        if e["type"] == "mentor.advisory_agg" and e["payload"]["issue"] == "thrashing"
    ]
    assert len(thr) == 1, "同 issue+target 只记一次事件"
    assert len(agg) == 9, "其后 9 轮各追加一条 advisory_agg"
    # occurrences 从账本现算（不投影进 meta，保 shadow/off 状态一致）：
    assert advisory_occurrences(view.events(from_seq=1))["thrashing:t-0001"] == 10
    # 重放一致：重开视图重算仍是 10
    again = StoreView(tmp_mission)
    assert advisory_occurrences(again.events(from_seq=1))["thrashing:t-0001"] == 10


# ── 1.5 ────────────────────────────────────────────────────────────


def test_readme_lists_known_limitations_exit_codes_and_terms():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for kw in (
        "planner-driven",
        "策略未数据化",
        "无跨客户隔离",
        "无评测基线",
        "无经验→技能写回",
        "spent_seconds 不可复现",
        "halted",
        "事件记忆",
        "长期记忆",
    ):
        assert kw in readme, f"README 已知限制/术语缺: {kw}"
