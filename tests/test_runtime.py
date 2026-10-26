"""S10 验收：主循环 / 调度器 / 预算 / 恢复（端到端，本地监听端口，不依赖外网）。

五条对应 S10 验收：
1. MockLLM 脚本能跑完整 mission：目标清单 → 派发 → 归并 → 收敛到 finished；
2. 步数预算耗尽 → 终态 budget_steps，不是崩溃；
3. 平台期：连续 8 轮无新线索 → 终态 plateau；
4. SECPT_DIE_AT_STEP 在第 N 步自毁后 resume，最终状态与不中断跑完全一致
   （含 meta 派生值；spent_seconds 墙钟不可重放，剔除后比对）；
5. scheduler 顺序：注入一个未判定 finding，本轮结束前已被裁决。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.graph.task import create_task
from pa.graph.ready import promote_pending
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Budget,
    FindingProposal,
    MissionSpec,
    MissionState,
    TaskDraft,
)
from pa.runtime import run
from pa.runtime import scheduler
from pa.store.event import apply_event
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


def _spec(port: int, *, steps: int = 12, evidence: str = "open") -> MissionSpec:
    return MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1", item="端口开放被证实", evidence=evidence, target="127.0.0.1"
            )
        ],
        budget=Budget(steps=steps, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{port}"],
    )


def _planner_script(port: int, evidence: str) -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{port}",
                    "success_criteria": [{"evidence": evidence}],
                    "scope_slice": {},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {"s": "v"},
            "rationale": "r",
        },
        ensure_ascii=False,
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


# ─────────────────────────── 1. 完整 mission 收敛 finished ───────────────────────────


def test_full_mission_converges_to_finished(tmp_mission, listener):
    llm = MockLLM(
        {
            "planner": [_planner_script(listener, "open")],
            "worker": [_worker_script(listener)],
        }
    )
    state = run(tmp_mission, _spec(listener), llm=llm, assets=_freeze())
    assert state.finished
    assert state.finish_reason == "completed"
    assert state.tasks["t-0001"].state == "done"
    events = [
        json.loads(line)
        for line in (tmp_mission / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    fin = [e for e in events if e["type"] == "mission.finished"]
    assert len(fin) == 1 and fin[0]["payload"]["reason"] == "completed"
    assert fin[0]["payload"]["satisfied"] == ["acc-1"]


# ─────────────────────────── 2. 步数预算耗尽 → budget_steps ───────────────────────────


def test_step_budget_exhaustion_terminates_budget_steps(tmp_mission, listener):
    # 判据永远不满足（evidence 串不在线索里）：任务反复重排队直到预算收口
    llm = MockLLM(
        {
            "planner": [_planner_script(listener, "never-ever-met")],
            "worker": [_worker_script(listener)],
        }
    )
    state = run(
        tmp_mission,
        _spec(listener, evidence="never-ever-met", steps=2),
        llm=llm,
        assets=_freeze(),
    )
    assert state.finished
    assert state.finish_reason == "budget_steps"
    assert not state.meta.get("fp_attempts")  # probe 成功结果永久缓存，无失败重试


# ─────────────────────────── 3. 平台期 → plateau ───────────────────────────


def test_plateau_terminates_immediately_when_queue_drained(tmp_mission, listener):
    # worker 第一步就 out_of_scope 被拦（10.255.255.1 不在 allowed_scope），
    # 任务 blocked 后队列排空（无 ready 且无 running）→ P1-1 立即 plateau，
    # 不再等 8 轮空烧预算（原 8 轮等待语义随本裁定废除）
    bad = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "10.255.255.1", "port": 80},
            "expect": "x",
        },
        ensure_ascii=False,
    )
    llm = MockLLM(
        {"planner": [_planner_script(listener, "never-ever-met")], "worker": [bad]}
    )
    state = run(
        tmp_mission,
        _spec(listener, evidence="never-ever-met"),
        llm=llm,
        assets=_freeze(),
    )
    assert state.finished
    assert state.finish_reason == "plateau"
    assert state.tasks["t-0001"].state == "blocked"
    assert state.step <= 2
    assert state.meta["plateau_reason"] == "no_ready"


# ─────────────────────────── 4. 断电钩子 + resume 一致性 ───────────────────────────

_RUNNER = """
import json, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from pa.models import MissionSpec
from pa.infra.llm import MockLLM
from pa.runtime import run
spec_json, script_json, root = sys.argv[1:4]
spec = MissionSpec.model_validate_json(spec_json) if spec_json else None
state = run(Path(root), spec, llm=MockLLM(json.loads(script_json)))
dump = state.model_dump(mode="json")
dump.pop("spent_seconds")            # 墙钟不可重放，一致性比对剔除
Path(root, "final.json").write_text(
    json.dumps(dump, sort_keys=True, ensure_ascii=False), encoding="utf-8")
print("finish_reason=" + state.finish_reason)
""".format(repo=str(ROOT))


def _spawn(args, env_extra=None):
    env = dict(os.environ)
    env.pop("SECPT_DIE_AT_STEP", None)
    env.pop("SECPT_LLM_MOCK", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
    )


def test_die_at_step_and_resume_matches_uninterrupted(tmp_mission, listener):
    # P1-1 后队列排空立即 plateau：任务 3 次尝试耗尽即排空，steps 预算必须
    # 在排空之前触顶（steps=3：第 3 步结算后 step=3 ≥ 3，先于 plateau 收口）
    spec = _spec(listener, evidence="never-ever-met", steps=3)
    spec_json = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)
    script = json.dumps(
        {
            "planner": [_planner_script(listener, "never-ever-met")],
            "worker": [_worker_script(listener)],
        },
        ensure_ascii=False,
    )

    # 不中断跑法（对照组）。两处 root 同名（basename m）：mission_id 取自
    # 目录名，同名才能逐字节比对
    ctl = tmp_mission / "ctl" / "m"
    r = _spawn([spec_json, script, str(ctl)])
    assert r.returncode == 0, r.stderr
    assert "finish_reason=budget_steps" in r.stdout

    # 断电跑法：第 2 步自毁（rc=9），再 resume 续跑
    die = tmp_mission / "die" / "m"
    r1 = _spawn([spec_json, script, str(die)], env_extra={"SECPT_DIE_AT_STEP": "2"})
    assert r1.returncode == 9, (r1.stdout, r1.stderr)
    r2 = _spawn(["", script, str(die)])  # 已存在目录：restore 路径，无需 spec
    assert r2.returncode == 0, r2.stderr
    assert "finish_reason=budget_steps" in r2.stdout

    a = (ctl / "final.json").read_text(encoding="utf-8")
    b = (die / "final.json").read_text(encoding="utf-8")
    assert a == b  # 含 meta 派生值逐字段一致


# ─────────────────────────── 5. JEV sweep：本轮内裁决 ───────────────────────────


def test_jev_sweep_adjudicates_injected_finding_same_round(tmp_mission, listener):
    (tmp_mission / "mission.json").write_text(
        json.dumps(_spec(listener).model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    writer = StoreWriter(tmp_mission)
    state = MissionState(spec=_spec(listener), mission_id=tmp_mission.name)
    task = create_task(
        state,
        writer,
        TaskDraft(
            type="recon",
            goal=f"probe 127.0.0.1:{listener}",
            success_criteria=[{"evidence": "open"}],
            scope_slice={},
            allowed_tools=["probe"],
            budget=Budget(steps=4),
        ),
    )
    promote_pending(state, writer)
    f = FindingProposal(
        claim="监听端口开放",
        severity="low",
        evidence_refs=[],
        reproduce="重跑 probe",
        falsify_if=["连接被拒绝"],
    )
    ev = writer.append(
        "finding.proposed",
        "worker:t-0001",
        {"finding": f.model_dump(mode="json")},
        "fp:test-1",
    )
    apply_event(state, ev)
    assert scheduler.finding_id(f) not in state.adjudications

    llm = MockLLM({"worker": [_worker_script(listener)]})
    scheduler.round(state, writer, llm, _freeze())

    fid = scheduler.finding_id(f)
    assert fid in state.adjudications  # 本轮结束前已裁决
    assert state.adjudications[fid].verdict == "suspected"  # 无证据引用
    assert state.tasks[task.id].state == "done"  # 正常派发未受影响
