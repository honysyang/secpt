"""命令行工作流验收：status 一览 / log 增强 / 家族目录解析 / do 指引。"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.cli import main as cli_main
from pa.infra.llm import MockLLM
from pa.report import overview
from pa.runtime import run

ROOT = Path(__file__).resolve().parent.parent


def _freeze():
    return AssetSnapshot.freeze(
        ROOT / "pa" / "core" / "prompts",
        [ROOT / "assets" / "skills", ROOT / "assets" / "playbooks"],
        [ROOT / "assets" / "roles"],
    )


@pytest.fixture
def listener() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


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
            "rationale": "r",
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


def _run(d: Path, port: int, *, steps=12, evidence="open"):
    return run(
        d,
        _spec(port, steps=steps, evidence=evidence),
        llm=MockLLM({"planner": [_planner(port, evidence)], "worker": [_worker(port)]}),
        assets=_freeze(),
    )


# ── secpt status ─────────────────────────────────────────────


def test_status_lists_families(tmp_path, monkeypatch, listener, capsys):
    monkeypatch.chdir(tmp_path)
    missions = tmp_path / "missions"
    _run(missions / "m-done" / "runs" / "1", listener)
    _run(missions / "m-stop" / "runs" / "1", listener, steps=2, evidence="never")

    rc = cli_main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "m-done" in out and "m-stop" in out
    assert "completed" in out and "budget_steps" in out
    assert "secpt log" in out, "底部应有深入指引"
    assert "RUNS" in out and "发现" in out
    # 只读：账本 sha 不变
    ledger = missions / "m-done" / "runs" / "1" / "events.jsonl"
    sha = hashlib.sha256(ledger.read_bytes()).hexdigest()
    cli_main(["status"])
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == sha


def test_status_empty_and_family_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli_main(["status"]) == 0
    assert "无 mission" in capsys.readouterr().out

    (tmp_path / "none").mkdir()
    # 传一个不存在的根：不炸，空表
    assert cli_main(["status", str(tmp_path / "none")]) == 0


def test_overview_latest_run_prefers_max_n(tmp_path):
    fam = tmp_path / "f"
    (fam / "runs" / "1").mkdir(parents=True)
    (fam / "runs" / "3" / "events.jsonl").mkdir(parents=True)
    (fam / "runs" / "2" / "events.jsonl").mkdir(parents=True)
    (fam / "runs" / "3" / "events.jsonl").touch()
    (fam / "runs" / "2" / "events.jsonl").touch()
    assert overview.latest_run(fam) == fam / "runs" / "3"
    assert overview.runs_count(fam) == 3
    # legacy 裸账本
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "events.jsonl").touch()
    assert overview.latest_run(legacy) == legacy and overview.runs_count(legacy) == 1
    # mixed：根账本(N=1) + runs/2 → 最新是 runs/2（do-4a1e630e 实例）
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "events.jsonl").touch()
    (mixed / "runs" / "2" / "events.jsonl").mkdir(parents=True)
    (mixed / "runs" / "2" / "events.jsonl").touch()
    assert overview.latest_run(mixed) == mixed / "runs" / "2"
    assert overview.runs_count(mixed) == 2
    assert overview.latest_run(tmp_path / "empty") is None


# ── secpt log 增强 ───────────────────────────────────────────


def test_log_flow_flag_offline_replay(tmp_mission, listener, capsys):
    _run(tmp_mission, listener)
    rc = cli_main(["log", str(tmp_mission), "--flow"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[planner]" in out and "[收口]" in out, "flow 重放应含叙事骨架"
    assert "secpt · " in out
    # 确定性：两次渲染逐字节一致
    cli_main(["log", str(tmp_mission), "--flow"])
    assert capsys.readouterr().out == out


def test_log_step_tokens_and_attempt_meta(tmp_mission, listener, capsys):
    _run(tmp_mission, listener, steps=2, evidence="never")
    cli_main(["log", str(tmp_mission)])
    out = capsys.readouterr().out
    assert "### 第 1 步 · mode=" in out
    assert "tokens" in out, "编排决策头应带该步 token 结算"
    assert "动作）" in out, "任务标题应带动作数"
    assert "尝试" in out


# ── 家族目录解析（trace / tui / stats） ──────────────────────


def test_family_dir_resolves_for_trace_and_stats(
    tmp_path, monkeypatch, listener, capsys
):
    family = tmp_path / "missions" / "fam"
    _run(family / "runs" / "1", listener)
    from pa.cli import _latest_link_to

    _latest_link_to(family, family / "runs" / "1")
    monkeypatch.chdir(tmp_path)

    assert cli_main(["trace", str(family)]) == 0
    assert "== 时间线 ==" in capsys.readouterr().out
    assert cli_main(["stats", str(family)]) == 0
    assert "MISSION" in capsys.readouterr().out


# ── do 面板验收 + 下一步指引 ─────────────────────────────────


def test_do_panel_shows_acceptance(tmp_path, monkeypatch, listener, capsys):
    monkeypatch.chdir(tmp_path)
    ok = json.dumps(
        {"items": [{"item": "端口开放", "evidence": "open"}]}, ensure_ascii=False
    )
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": [ok]}))
    assert cli_main(["do", f"扫描 127.0.0.1:{listener} 的端口", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "验收：" in out and "acc-1" in out, "确认面板必须展示收口合同"
    assert "@ 127.0.0.1" in out


def test_do_prints_next_hints(tmp_path, monkeypatch, listener, capsys):
    monkeypatch.chdir(tmp_path)
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
    ok = json.dumps(
        {"items": [{"item": "端口开放", "evidence": "open"}]}, ensure_ascii=False
    )
    monkeypatch.setenv(
        "SECPT_LLM_MOCK",
        json.dumps({"do": [ok], "planner": [planner], "worker": [probe]}),
    )
    rc = cli_main(
        ["do", f"扫描 127.0.0.1:{listener} 的端口", "--dir", "missions/x", "--yes"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "日志：  secpt log missions/x" in out
    assert "续跑：  secpt resume missions/x" in out
    assert "secpt status missions" in out
