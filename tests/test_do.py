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

from pa.cli import main as cli_main
from pa.core import nl
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
    targets = nl.extract_targets("扫描 10.0.0.5 的 80 和 443 端口，找未授权访问")
    assert targets == ["10.0.0.5:80", "10.0.0.5:443"]


def test_do_without_targets_exits_1_and_no_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # LLM 即使想编目标也无门：build_spec 在调 LLM 之前就拒绝
    rc = cli_main(["do", "随便看看有没有问题", "--yes"])
    assert rc == 1
    assert capsys.readouterr().err != "" or rc == 1
    assert not (tmp_path / "missions").exists() or not list(
        (tmp_path / "missions").glob("do-*")
    ), "不许建目录"


def test_fabricated_evidence_dropped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fab = json.dumps(
        {
            "items": [
                {"item": "存在未授权访问", "evidence": "unauthorized_access_confirmed"},
                {"item": "端口开放", "evidence": "open"},
            ]
        },
        ensure_ascii=False,
    )
    llm = MockLLM({"do": [fab]})
    spec, warnings = nl.build_spec("扫描 10.0.0.5 找未授权访问", llm=llm)
    assert len(spec.acceptance) == 1
    assert spec.acceptance[0].evidence == "open"
    assert any("白名单" in w for w in warnings), "编造证据要有丢弃警告"


def test_dry_run_and_unconfirmed_write_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ok = json.dumps(
        {"items": [{"item": "端口开放", "evidence": "open"}]}, ensure_ascii=False
    )
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": [ok]}))

    rc = cli_main(["do", "扫描 127.0.0.1:9 的端口", "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "missions").exists(), "dry-run 不得建目录"

    # 未确认（EOF → 空串 → 取消）：不建目录、零事件
    rc = cli_main(["do", "扫描 127.0.0.1:9 的端口"])
    assert rc == 1
    missions = tmp_path / "missions"
    assert not missions.exists() or not list(missions.rglob("events.jsonl"))


def test_do_runs_mission_end_to_end(tmp_path, monkeypatch, listener, capsys):
    """do 默认实时渲染流程叙事：planner 计划 → orchestrator 编排 →
    worker 思考/行动/观察 → 判定 → 收口，而不是平铺 payload。"""
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
    assert rc == 0
    out = capsys.readouterr().out
    assert "[planner]" in out, "默认流程日志应展示 planner 计划"
    assert "orchestrator 编排" in out, "应展示每轮编排决策"
    assert "[思考]" in out and "预期: open" in out, "worker 的预期/理由应可读"
    assert "[行动] probe" in out and "[观察]" in out, "应展示动作与观察"
    assert "[判定] done" in out and "success" in out
    assert "[收口]" in out and "completed" in out
    assert "[action.started]" not in out, "默认不再平铺原始事件"
    # 第一次 do 家族根还没有账本：events.jsonl 落到 runs/1（_bootstrap 会
    # mkdir runs/1 再写），latest 指向 runs/1
    d = tmp_path / "missions" / "x" / "runs" / "1"
    assert (d / "mission.json").exists()
    assert (d / "events.jsonl").exists()
    spec = json.loads((d / "mission.json").read_text(encoding="utf-8"))
    assert spec["targets"] == [f"127.0.0.1:{listener}"]
    latest = tmp_path / "missions" / "x" / "latest"
    assert latest.readlink() == Path("runs/1")


def test_llm_cannot_expand_scope(tmp_path, monkeypatch):
    """LLM 在验收里塞范围外目标 8.8.8.8：整条丢弃 + 警告点名；
    最终 allowed_scope 只有 10.0.0.5:80。"""
    monkeypatch.chdir(tmp_path)
    fab = json.dumps(
        {
            "items": [
                {"item": "8.8.8.8 的 80 端口存在未授权访问", "evidence": "open"},
                {"item": "10.0.0.5:80 端口开放", "evidence": "probe.open"},
            ]
        },
        ensure_ascii=False,
    )
    llm = MockLLM({"do": [fab]})
    spec, warnings = nl.build_spec("扫描 10.0.0.5:80 找未授权访问", llm=llm)
    assert spec.allowed_scope == ["10.0.0.5:80"]
    assert all("8.8.8.8" not in (a.item + a.evidence) for a in spec.acceptance)
    assert any("8.8.8.8" in w for w in warnings), "警告必须点名被丢弃目标"
    # 合法项保留（probe.open 是新点分枚举）
    assert any(a.evidence == "probe.open" for a in spec.acceptance)


def test_targets_adjacent_to_cjk_extracted():
    """回归：\b 在 Unicode 模式下对中英交界失效（「扫描aimohu.com」
    提取为空）。域名/IP 贴着汉字、全角标点也必须抽得出。"""
    assert nl.extract_targets("扫描aimohu.com ，评估安全性") == ["aimohu.com"]
    assert nl.extract_targets("扫描10.0.0.5的80和443端口") == [
        "10.0.0.5:80",
        "10.0.0.5:443",
    ]
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
    for text in (
        "版本号1.2.3.4刚发布",
        "网段10.0.0.0/33不存在",
        "电话13800138000",
        "得分99.5分",
        "v1.2.3.4.5不是IP",
    ):
        assert nl.extract_targets(text) == [], f"{text!r} 不应提取出目标"


def test_attachment_targets_and_panel(tmp_path, monkeypatch, capsys):
    """附件接收：目标抽在「任务文本 + 附件内容」合并语料上——任务文本
    只指方向，附件列出的 host:port 进 targets 与 allowed_scope；
    objective 只保留任务文本；确认面板列出附件路径。"""
    monkeypatch.chdir(tmp_path)
    att = tmp_path / "scope.txt"
    att.write_text(
        "授权范围：\nhttp://example.com:8080/admin 路径与端口\n10.10.0.5 的 443 端口\n",
        encoding="utf-8",
    )
    ok = json.dumps(
        {"items": [{"item": "端口开放", "evidence": "open"}]}, ensure_ascii=False
    )
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": [ok]}))
    rc = cli_main(
        ["do", "按附件里的授权范围扫描", "--attachment", str(att), "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert str(att) in out, "确认面板应列出附件"
    spec_match = [ln for ln in out.splitlines() if ln.startswith("目标：")]
    assert spec_match
    assert "10.10.0.5:443" in spec_match[0], "附件里的目标应被抽进 targets"
    assert "example.com:8080" in spec_match[0]


def test_attachment_missing_fails_fast_before_llm(tmp_path, monkeypatch):
    """附件不存在 → 退出 1：校验在任何 LLM 调用之前，失败要快。"""
    monkeypatch.chdir(tmp_path)
    # 故意只注入 do 脚本但会让其失败也无妨——重点是不该走到那里
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": ["{}"]}))
    rc = cli_main(
        [
            "do",
            "扫描 10.0.0.5",
            "--attachment",
            str(tmp_path / "no-such-file.txt"),
            "--yes",
        ]
    )
    assert rc == 1
    assert not (tmp_path / "missions").exists()


def test_do_repeated_runs_family_runs_n(tmp_path, monkeypatch, listener, capsys):
    """同一任务 do 两次：第一次写家族根（旧裸 hash N=1），第二次自动开
    runs/2；latest 符号链接始终指向最新一次 run，report/watch/resume
    都从 latest 解析，不需要记 runs/N 的具体编号。"""
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
    assert rc == 0
    family = tmp_path / "missions" / "x"
    # 第一次 do：家族根还没有账本，events.jsonl 落到 runs/1（_bootstrap 会
    # mkdir runs/1 再写），latest 指向 runs/1
    assert (family / "runs" / "1" / "events.jsonl").exists(), "第一次 do 应写 runs/1"
    latest = family / "latest"
    assert latest.is_symlink(), "第一次 do 后 latest 应存在"
    assert latest.readlink() == Path("runs/1"), "第一次 do 后 latest 应指向 runs/1"
    # 第二次 do：家族根已有 runs/1 账本但家族根本身没有，自动开 runs/2
    rc = cli_main(
        ["do", f"扫描 127.0.0.1:{listener} 的端口", "--dir", "missions/x", "--yes"]
    )
    assert rc == 0
    assert (family / "runs" / "2" / "events.jsonl").exists(), "第二次 do 应开 runs/2"
    assert (family / "runs" / "1" / "events.jsonl").exists(), "runs/1 不应被覆盖"
    assert latest.readlink() == Path("runs/2"), "latest 应指向最新一次 runs/N"
    assert (family / "report.md").exists(), "家族根 report.md 应聚合所有 run"
