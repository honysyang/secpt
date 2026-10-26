"""编排权迁移（decide）验收：orchestrate 契约 / 确定性护栏 / 静默降级。

覆盖六条关键路径：
1. 护栏裁剪：run 里坏 id（不存在 / 非 ready / 重复）/ 超批 → 被剔除，
   好引用保留且顺序不乱；
2. create 上限：一轮 >6 条草案截断到 CREATE_CAP，结构缺陷草案被
   coach（enforce 档）拦掉；
3. 静默降级：MockLLM 未注入 orchestrator phase → ModelCallError →
   fallback_directive（缩编前确定性批调度语义）接管，不抛错；
4. 死锁防线：run 与 create 双空的指令被 fallback:empty 接管；
5. 幂等重放：同一轮重复 orchestrate 不产生第二份任务/审计事件；
6. 端到端一行通：round() 下 directive 驱动派发，worker 正常跑完。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.core.planner import orchestrate
from pa.graph.ready import promote_pending
from pa.graph.task import create_task
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Budget,
    MissionSpec,
    MissionState,
    TaskDraft,
)
from pa.runtime import scheduler
from pa.store.view import StoreView
from pa.store.writer import StoreWriter

ROOT = Path(__file__).resolve().parent.parent


def _freeze():
    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])


@pytest.fixture
def listener():
    """真实 TCP 监听端口：worker probe 走真网关，端口必须在 allowed_scope。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


@pytest.fixture
def mission(listener, tmp_mission) -> tuple[MissionState, StoreWriter]:
    """三任务三 ready 的事先编排：默认 max_workers=2（护栏批上限可见）。"""
    spec = MissionSpec(
        objective="探测本地端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1", item="端口证实", evidence="open", target="127.0.0.1"
            )
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{listener}"],
        max_workers=2,
    )
    writer = StoreWriter(tmp_mission)
    state = MissionState(spec=spec, mission_id=tmp_mission.name)
    for goal in tuple(f"probe 127.0.0.1:{listener}" for _ in range(3)):
        create_task(
            state,
            writer,
            TaskDraft(
                type="recon",
                goal=goal,
                success_criteria=[{"evidence": "open"}],
                scope_slice={"tgt": goal},
                allowed_tools=["probe"],
            ),
        )
    promote_pending(state, writer)
    return state, writer


def _directive(raw: dict[str, Any]) -> str:
    return json.dumps(raw, ensure_ascii=False)


def _draft(goal: str) -> dict[str, Any]:
    return {
        "type": "recon",
        "goal": goal,
        "success_criteria": [{"evidence": "open"}],
        "scope_slice": {"tgt": goal},
        "allowed_tools": ["probe"],
    }


# ─────────────────────── 1. run 护栏：坏引用剔除 / 批上限 / 顺序 ───────────────────────


def test_guard_run_drops_bad_ids_and_caps_batch(mission):
    state, writer = mission
    llm = MockLLM(
        {
            "orchestrator": [
                _directive(
                    {
                        "run": ["t-9999", "t-0002", "t-0002", "t-0001", "t-0003"],
                        "create": [],
                        "rationale": "x",
                    }
                )
            ]
        }
    )
    batch = orchestrate(state, writer, llm, _freeze())
    # t-9999 不存在剔除；t-0002 重复去重；批上限 max_workers=2 截断，
    # 指令顺序 != ready 优先级顺序 → 护栏尊重指令者：t-0002、t-0001
    assert [t.id for t in batch] == ["t-0002", "t-0001"]
    ev = json.loads(_last_event(writer, "decision.made"))
    assert ev["payload"]["run"] == ["t-0002", "t-0001"]
    assert ev["payload"]["mode"] == "orchestrator"


def test_abandon_supersedes_and_survives_requeue(mission):
    """abandon 是编排者对自动重排队的否决权：任务转 superseded，不再 run。"""
    state, writer = mission
    llm = MockLLM(
        {
            "orchestrator": [
                _directive(
                    {
                        "run": ["t-0001"],
                        "abandon": ["t-0001"],
                        "create": [],
                        "rationale": "t-0001 假设已死",
                    }
                )
            ]
        }
    )
    batch = orchestrate(state, writer, llm, _freeze())
    assert state.tasks["t-0001"].state == "superseded"
    assert all(t.id != "t-0001" for t in batch)
    ev = json.loads(_last_event(writer, "decision.made"))
    assert ev["payload"]["abandoned"] == ["t-0001"]


# ─────────────────────── 2. create 护栏：上限截断 / enforce 拦缺陷 ───────────────────────


def test_guard_create_caps_and_drops_defects(mission, monkeypatch):
    state, writer = mission
    monkeypatch.setenv("SECPT_MENTOR_MODE", "enforce")
    drafts = [_draft(f"goal-{i}") for i in range(8)]
    # 前 5 个没有 scope_slice（enforce 档 I4 拦截），后 3 个合格——截断
    # 先于 coach，8 条只取前 6 条（goal-0..5）进护栏
    for d in drafts[:5]:
        d.pop("scope_slice")
    llm = MockLLM(
        {"orchestrator": [_directive({"run": [], "create": drafts, "rationale": "x"})]}
    )
    _ = orchestrate(state, writer, llm, _freeze())
    created = [
        t
        for t in state.tasks.values()
        if t.id not in ("t-0001", "t-0002", "t-0003") and t.state == "pending"
    ]
    assert state.task_seq == 3 + 1  # 窗口内仅 goal-5 合格
    # goal-5 被建出；goal-6/7 合格但已被截断丢弃（本轮只到 +CREATE_CAP）
    assert {t.goal for t in created} == {"goal-5"}
    adv = [
        e
        for e in StoreView(writer.root).events(from_seq=1)
        if e.type == "mentor.advisory"
    ]
    assert {e.payload["issue"] for e in adv} == {"empty_scope"}


# ─────────────────────── 3. 静默降级：模型缺席 → 兜底批 ───────────────────────


def test_silent_fallback_on_missing_phase(mission):
    state, writer = mission
    llm = MockLLM({})  # orchestrator 未注入 → ModelCallError
    batch = orchestrate(state, writer, llm, _freeze())  # 不抛
    assert [t.id for t in batch] == ["t-0001", "t-0002"]  # ready 优先级批，截断 2
    ev = json.loads(_last_event(writer, "decision.made"))
    assert ev["payload"]["mode"] == "fallback:ModelCallError"
    assert "降级" in ev["payload"]["rationale"]


# ─────────────────────── 4. 空指令死锁防线 ───────────────────────


def test_empty_directive_falls_back(mission):
    state, writer = mission
    llm = MockLLM(
        {"orchestrator": [_directive({"run": [], "create": [], "rationale": "先观望"})]}
    )
    batch = orchestrate(state, writer, llm, _freeze())
    assert [t.id for t in batch] == ["t-0001", "t-0002"]
    assert (
        json.loads(_last_event(writer, "decision.made"))["payload"]["mode"]
        == "fallback:empty"
    )


# ─────────────────────── 5. 幂等重放：同轮重复编排不双记 ───────────────────────


def test_orchestrate_is_idempotent_per_round(mission):
    state, writer = mission
    llm = MockLLM(
        {
            "orchestrator": [
                _directive(
                    {"run": ["t-0001"], "create": [_draft("extra")], "rationale": "x"}
                )
            ]
        }
    )
    first = orchestrate(state, writer, llm, _freeze())
    n_events = writer._seq
    second = orchestrate(state, writer, llm, _freeze())
    assert [t.id for t in second] == [t.id for t in first] == ["t-0001"]
    assert state.task_seq == 4  # extra 任务没有重复创建
    assert writer._seq == n_events  # 审计事件没有重复写入


# ─────────────────────── 6. round() 端到端：directive 驱动派发 ───────────────────────


def test_round_dispatches_accomplishes_task(mission):
    state, writer = mission
    port = int(state.spec.allowed_scope[0].partition(":")[2])
    llm = MockLLM(
        {
            "orchestrator": [
                _directive({"run": ["t-0001"], "create": [], "rationale": "先探监听口"})
            ],
            "worker": [
                json.dumps(
                    {
                        "kind": "call_tool",
                        "tool": "probe",
                        "args": {"target": "127.0.0.1", "port": port},
                        "expect": "端口状态",
                    },
                    ensure_ascii=False,
                )
            ],
        }
    )
    scheduler.round(state, writer, llm, _freeze())
    assert state.tasks["t-0001"].state in ("done", "failed")
    # 指令没让 t-0002 跑，它仍是 ready——编排权在指令者手里
    assert state.tasks["t-0002"].state == "ready"
    assert not state.finished


def _last_event(
    writer: StoreWriter, ev_type: str, allow_missing: bool = False
) -> str | None:
    seen = [e for e in StoreView(writer.root).events(from_seq=1) if e.type == ev_type]
    if not seen:
        if allow_missing:
            return None
        pytest.fail(f"账本里没有 {ev_type} 事件")
    return seen[-1].model_dump_json()
