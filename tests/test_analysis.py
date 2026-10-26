"""运行分析文档（analysis.md）验收：任意终态自动落盘、确定性、可补生成。"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.infra.llm import MockLLM
from pa.report.analysis import render_analysis
from pa.report.log import render_log
from pa.runtime import run

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
    return AssetSnapshot.freeze(
        ROOT / "pa" / "core" / "prompts",
        [ROOT / "assets" / "skills", ROOT / "assets" / "playbooks"],
        [ROOT / "assets" / "roles"],
    )


def _spec(port: int, *, steps: int = 12, evidence: str = "open"):
    from pa.models import AcceptanceItem, Budget, MissionSpec

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


def _planner(port: int, evidence: str) -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{port}",
                    "success_criteria": [{"evidence": evidence}],
                    "scope_slice": {"host": "127.0.0.1"},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {},
            "rationale": "先探锚点",
        },
        ensure_ascii=False,
    )


def _worker(port: int) -> str:
    return json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": port},
            "expect": "open",
        },
        ensure_ascii=False,
    )


def test_analysis_written_on_completed(tmp_mission, listener):
    """正常收口：analysis.md 与 report.md 同在，结构齐全、确定性。"""
    run(
        tmp_mission,
        _spec(listener),
        llm=MockLLM({"planner": [_planner(listener, "open")], "worker": [_worker(listener)]}),
        assets=_freeze(),
    )
    analysis = tmp_mission / "analysis.md"
    assert analysis.exists(), "每次运行必须自动落盘分析文档"
    assert (tmp_mission / "report.md").exists()
    text = analysis.read_text(encoding="utf-8")
    for sec in (
        "# secpt 运行分析",
        "## 运行元信息",
        "## 系统信号",
        "## 优化建议",
        "## 失败归因",
        "## 运行过程全记录",
        "## 编排决策",
        "## 收口",
    ):
        assert sec in text, f"分析文档缺 {sec}"
    assert "先探锚点" in text, "编排理由应进入全过程记录"
    assert "probe" in text
    # 确定性：重新渲染逐字节一致（无 now()/随机数）
    from pa.store.view import StoreView

    state = StoreView(tmp_mission).restore()
    assert render_analysis(state, tmp_mission) == text


def test_analysis_written_on_plateau_with_attribution(tmp_mission, listener):
    """失败运行恰恰是优化素材：plateau 也要落盘，失败归因要能点名任务。"""
    run(
        tmp_mission,
        _spec(listener, steps=2, evidence="never-met"),
        llm=MockLLM({"planner": [_planner(listener, "never-met")], "worker": [_worker(listener)]}),
        assets=_freeze(),
    )
    analysis = tmp_mission / "analysis.md"
    assert analysis.exists()
    text = analysis.read_text(encoding="utf-8")
    assert "终态：budget_steps" in text or "终态：plateau" in text
    assert "## 失败归因" in text
    assert "t-0001" in text, "归因必须点名到任务"
    assert "## 优化建议" in text


def test_analysis_regen_via_report_cli(tmp_mission, listener, capsys, monkeypatch):
    """secpt report 一键补两份文档；analysis.md 确定性可复算。"""
    from pa.cli import main as cli_main

    monkeypatch.setenv("SECPT_MENTOR_MODE", "off")
    run(
        tmp_mission,
        _spec(listener),
        llm=MockLLM({"planner": [_planner(listener, "open")], "worker": [_worker(listener)]}),
        assets=_freeze(),
    )
    (tmp_mission / "analysis.md").unlink()
    assert cli_main(["report", str(tmp_mission)]) == 0
    out = capsys.readouterr().out
    assert "报告已写入" in out and "运行分析已写入" in out
    text = (tmp_mission / "analysis.md").read_text(encoding="utf-8")
    # 与 render_log 同源：运行过程段必须包含思考链
    assert "预期：open" in text


def test_analysis_narrative_matches_log(tmp_mission, listener):
    """单一事实源：analysis 的运行过程段 == render_log 去标题。"""
    run(
        tmp_mission,
        _spec(listener),
        llm=MockLLM({"planner": [_planner(listener, "open")], "worker": [_worker(listener)]}),
        assets=_freeze(),
    )
    text = (tmp_mission / "analysis.md").read_text(encoding="utf-8")
    log_body = render_log(tmp_mission).split("\n", 1)[1].lstrip("\n")
    assert log_body in text
