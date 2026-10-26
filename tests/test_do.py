"""Part 2（secpt do）验收测试。

- 端口列表展开：'扫描 10.0.0.5 的 80 和 443 端口，找未授权访问'
  → targets == [10.0.0.5:80, 10.0.0.5:443]；
- 无目标 → 返回 1，且不建目录；
- 编造 evidence → 被丢弃不报错，全丢弃落默认验收；
- --dry-run / 未确认 → 不写 mission.json、零事件。
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from pa import nl
from pa.cli import main as cli_main
from pa.infra.llm import MockLLM

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def listener() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


def test_port_clause_expansion():
    targets = nl.extract_targets("扫描 10.0.0.5 的 80 和 443 端口，"
                                 "找未授权访问")
    assert targets == ["10.0.0.5:80", "10.0.0.5:443"]


def test_do_without_targets_exits_1_and_no_dir(tmp_path, monkeypatch,
                                               capsys):
    monkeypatch.chdir(tmp_path)
    # LLM 即使想编目标也无门：build_spec 在调 LLM 之前就拒绝
    rc = cli_main(["do", "随便看看有没有问题", "--yes"])
    assert rc == 1
    assert capsys.readouterr().err != "" or rc == 1
    assert not (tmp_path / "missions").exists() or \
        not list((tmp_path / "missions").glob("do-*")), "不许建目录"


def test_fabricated_evidence_dropped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fab = json.dumps({"items": [
        {"item": "存在未授权访问", "evidence": "unauthorized_access_confirmed"},
        {"item": "端口开放", "evidence": "open"}]}, ensure_ascii=False)
    llm = MockLLM({"do": [fab]})
    spec, warnings = nl.build_spec("扫描 10.0.0.5 找未授权访问", llm=llm)
    assert len(spec.acceptance) == 1
    assert spec.acceptance[0].evidence == "open"
    assert any("白名单" in w for w in warnings), "编造证据要有丢弃警告"


def test_dry_run_and_unconfirmed_write_nothing(tmp_path, monkeypatch,
                                               capsys):
    monkeypatch.chdir(tmp_path)
    ok = json.dumps({"items": [{"item": "端口开放", "evidence": "open"}]},
                    ensure_ascii=False)
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": [ok]}))

    rc = cli_main(["do", "扫描 127.0.0.1:9 的端口", "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "missions").exists(), "dry-run 不得建目录"

    # 未确认（EOF → 空串 → 取消）：不建目录、零事件
    rc = cli_main(["do", "扫描 127.0.0.1:9 的端口"])
    assert rc == 1
    missions = tmp_path / "missions"
    assert not missions.exists() or not list(missions.rglob("events.jsonl"))


def test_do_runs_mission_end_to_end(tmp_path, monkeypatch, listener):
    monkeypatch.chdir(tmp_path)
    probe = json.dumps({"kind": "call_tool", "tool": "probe",
                        "args": {"target": "127.0.0.1", "port": listener},
                        "expect": "open"}, ensure_ascii=False)
    planner = json.dumps({"tasks": [{"type": "recon",
        "goal": f"probe 127.0.0.1:{listener}",
        "success_criteria": [{"evidence": "open"}],
        "scope_slice": {"host": "127.0.0.1"}, "allowed_tools": ["probe"],
        "budget": {"steps": 4}}], "current_strategy": {}, "rationale": "r"},
        ensure_ascii=False)
    ok = json.dumps({"items": [{"item": "端口开放", "evidence": "open"}]},
                    ensure_ascii=False)
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps(
        {"do": [ok], "planner": [planner], "worker": [probe]}))
    rc = cli_main(["do", f"扫描 127.0.0.1:{listener} 的端口",
                   "--dir", "missions/x", "--yes"])
    assert rc == 0
    d = tmp_path / "missions" / "x"
    assert (d / "mission.json").exists()
    assert (d / "events.jsonl").exists()
    spec = json.loads((d / "mission.json").read_text(encoding="utf-8"))
    assert spec["targets"] == [f"127.0.0.1:{listener}"]


def test_llm_cannot_expand_scope(tmp_path, monkeypatch):
    """LLM 在验收里塞范围外目标 8.8.8.8：整条丢弃 + 警告点名；
    最终 allowed_scope 只有 10.0.0.5:80。"""
    monkeypatch.chdir(tmp_path)
    fab = json.dumps({"items": [
        {"item": "8.8.8.8 的 80 端口存在未授权访问", "evidence": "open"},
        {"item": "10.0.0.5:80 端口开放", "evidence": "probe.open"},
    ]}, ensure_ascii=False)
    llm = MockLLM({"do": [fab]})
    spec, warnings = nl.build_spec("扫描 10.0.0.5:80 找未授权访问", llm=llm)
    assert spec.allowed_scope == ["10.0.0.5:80"]
    assert all("8.8.8.8" not in (a.item + a.evidence)
               for a in spec.acceptance)
    assert any("8.8.8.8" in w for w in warnings), "警告必须点名被丢弃目标"
    # 合法项保留（probe.open 是新点分枚举）
    assert any(a.evidence == "probe.open" for a in spec.acceptance)


def test_targets_adjacent_to_cjk_extracted():
    """回归：\b 在 Unicode 模式下对中英交界失效（「扫描aimohu.com」
    提取为空）。域名/IP 贴着汉字、全角标点也必须抽得出。"""
    assert nl.extract_targets("扫描aimohu.com ，评估安全性") == ["aimohu.com"]
    assert nl.extract_targets("扫描10.0.0.5的80和443端口") == \
        ["10.0.0.5:80", "10.0.0.5:443"]
    assert nl.extract_targets("检查v1.2.3.4.5版本串不是目标") == []


def test_weird_natural_language_formats():
    """自然语言怪异形态矩阵：全角、单端口、后置端口、换行、大小写。"""
    cases = [
        # (输入, 期望 targets)
        ("扫描１２７．０．０．１：８０８０", ["127.0.0.1:8080"]),
        ("扫描10.0.0.5的80端口", ["10.0.0.5:80"]),
        ("检查 10.0.0.5，端口：80 和 443", ["10.0.0.5:80", "10.0.0.5:443"]),
        ("扫描AIMOHU.COM的\n8080\n端口", ["AIMOHU.COM:8080"]),
        ("看看１０．０．０．５　的　８０　端口", ["10.0.0.5:80"]),
        ("端口 22 和 3306 的 10.0.0.5", ["10.0.0.5:22", "10.0.0.5:3306"]),
        ("扫描10.0.0.5（80端口）", ["10.0.0.5:80"]),
    ]
    for text, want in cases:
        got = nl.extract_targets(text)
        assert got == want, f"{text!r}: {got} != {want}"


def test_adversarial_non_targets_not_extracted():
    """对抗样本：版本号、非法 CIDR、长数字不是目标。"""
    for text in ("版本号1.2.3.4刚发布", "网段10.0.0.0/33不存在",
                 "电话13800138000", "得分99.5分", "v1.2.3.4.5不是IP"):
        assert nl.extract_targets(text) == [], f"{text!r} 不应提取出目标"


def test_do_refuses_existing_ledger(tmp_path, monkeypatch, capsys):
    """do 只引导新 mission：目录已有账本必须拒绝（退出 1），不静默续跑。"""
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "missions" / "occupied"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("", encoding="utf-8")
    rc = cli_main(["do", "扫描 10.0.0.5:80", "--dir", str(d), "--yes"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "已有事件账本" in (captured.out + captured.err)
    assert (d / "events.jsonl").read_text(encoding="utf-8") == "", \
        "拒绝时不得写账本"
