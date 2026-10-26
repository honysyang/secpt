"""M3 出口审计（WORKFLOW §6-2 / §7 M3）：evidence 完整性校验进交付门。

1. audit_refs（pa/core/audit.py）：纯检查——findings 引用计数与断链清单
   确定性可复算；confirmed 含断链引用显式亮出 confirmed_broken（不吞信号）。
2. exit_audit：mission.audit 事件落账幂等（ma:{mission_id} 只落一次）。
3. 交付门接线：_finish 在 mission.finished 之前落 audit 事件；报告概述
   带「证据审计」行（render 重算，确定性红线不破）。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from pa.adjudicate import finding_id
from pa.assets.snapshot import AssetSnapshot
from pa.core.audit import audit_refs, exit_audit
from pa.infra import evidence
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Adjudication,
    Budget,
    FindingProposal,
    MissionSpec,
    MissionState,
)
from pa.report import render_report
from pa.runtime import run
from pa.store.writer import StoreWriter
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


def _spec() -> MissionSpec:
    return MissionSpec(
        objective="审计口径验证",
        targets=["10.0.0.5:80"],
        acceptance=[
            AcceptanceItem(
                id="a1", item="端口开放", evidence="port.open", target="10.0.0.5"
            )
        ],
        budget=Budget(),
        allowed_scope=["10.0.0.0/24:80"],
    )


def _event_types(root: Path) -> list[str]:
    path = root / "events.jsonl"
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _state(tmp: Path, root: Path) -> MissionState:
    """三个提案：全证实（confirmed）/ 部分断链（suspected）/ 全断链。"""
    s = MissionState(spec=_spec(), mission_id=tmp.name)
    e_ok = evidence.put(root, b"a-output", task_id="t-1")
    e_ok2 = evidence.put(root, b"b-output", task_id="t-1")
    e_partial = evidence.put(root, b"c-output", task_id="t-2")
    f_ok = FindingProposal(
        claim="已证实端口开放",
        severity="high",
        evidence_refs=[e_ok, e_ok2],
        reproduce="重跑 probe",
        falsify_if=["拒绝连接"],
    )
    f_partial = FindingProposal(
        claim="部分断链的引用",
        severity="med",
        evidence_refs=[e_partial, "ev-" + "0" * 16],
        reproduce="重跑 probe",
        falsify_if=["无输出"],
    )
    all_broken = FindingProposal(
        claim="全断链（随裁决落 false_positive）",
        severity="low",
        evidence_refs=["ev-" + "f" * 16],
        reproduce="无法复现",
        falsify_if=["一切正常"],
    )
    for i, f in enumerate((f_ok, f_partial, all_broken)):
        s.findings.append(f)
        s.adjudications[finding_id(f)] = Adjudication(
            finding_id=finding_id(f),
            verdict="confirmed" if i == 0 else "suspected",
            reason="测试注入",
        )
    return s


def test_audit_refs_counts_and_broken_detail(tmp_mission):
    root = tmp_mission / "ev-root"
    root.mkdir()
    state = _state(tmp_mission, root)
    r = audit_refs(state, root)
    # 3 个提案共 5 条引用：可核验 3（各 evidence.put 内容真实存在），断链 2
    assert r["refs"] == 5
    assert r["verified"] == 3
    assert {b["claim"] for b in r["broken"]} == {
        "部分断链的引用",
        "全断链（随裁决落 false_positive）",
    }
    assert r["confirmed_broken"] == []


def test_audit_refs_flags_confirmed_with_broken_refs(tmp_mission):
    root = tmp_mission / "ev-root"
    root.mkdir()
    state = _state(tmp_mission, root)
    # f_ok 注入一条断链引用：引用内容变了 → finding_id 随内容漂移，旧
    # adjudication 失配会掩盖断链信号——按当前内容重挂 confirmed 再审计
    f_ok = state.findings[0]
    f_ok.evidence_refs.append("ev-" + "3" * 16)
    fid = finding_id(f_ok)
    state.adjudications.clear()
    state.adjudications[fid] = Adjudication(
        finding_id=fid, verdict="confirmed", reason="口径检查"
    )
    r = audit_refs(state, root)
    assert fid in r["confirmed_broken"]


def test_audit_refs_empty_is_zero(tmp_mission):
    tmp = tmp_mission / "rootless"
    tmp.mkdir()
    s = MissionState(spec=_spec(), mission_id=tmp.name)
    r = audit_refs(s, tmp)
    assert r == {
        "findings": 0,
        "refs": 0,
        "verified": 0,
        "broken": [],
        "confirmed_broken": [],
    }


def test_audit_refs_deterministic(tmp_mission):
    root = tmp_mission / "ev-root"
    root.mkdir()
    state = _state(tmp_mission, root)
    assert audit_refs(state, root) == audit_refs(state, root)


def test_exit_audit_appends_once(tmp_mission):
    root = tmp_mission / "m-once"
    root.mkdir()
    writer = StoreWriter(root)
    state = _state(tmp_mission, root)
    assert exit_audit(state, writer) == exit_audit(state, writer)
    assert _event_types(root).count("mission.audit") == 1


def _freeze():
    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])


def _run_finished(tmp_mission, port: int) -> None:
    """复用 test_report 同款 MockLLM 场景拉起一个真收口的 mission。"""
    spec = MissionSpec(
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
    llm = MockLLM(
        {
            "planner": [
                json.dumps(
                    {
                        "tasks": [
                            {
                                "type": "recon",
                                "goal": f"probe 127.0.0.1:{port}",
                                "success_criteria": [{"evidence": "open"}],
                                "scope_slice": {},
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
            "worker": [
                json.dumps(
                    {
                        "kind": "call_tool",
                        "tool": "probe",
                        "args": {"target": "127.0.0.1", "port": port},
                        "expect": "端口开放",
                    },
                    ensure_ascii=False,
                )
            ],
        }
    )
    run(tmp_mission, spec, llm=llm, assets=_freeze())


def test_finish_appends_audit_before_finished(tmp_mission, listener):
    _run_finished(tmp_mission, listener)
    types = _event_types(tmp_mission)
    assert "mission.audit" in types
    assert types.index("mission.audit") < types.index("mission.finished")


def test_report_contains_audit_line(tmp_mission, listener):
    _run_finished(tmp_mission, listener)
    state = StoreView(tmp_mission).restore()
    text = render_report(state, StoreView(tmp_mission))
    assert "证据审计" in text
