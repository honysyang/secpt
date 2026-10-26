"""S11 验收：报告（确定性 / 正文与附录 / 证据链可核验 / 凭证脱敏）与 CLI 退出码。

- 同一 state 渲染两次 == 逐字节相等（约束 B）；
- 注入 suspected + false_positive：不在正文、在附录，false_positive 写明
  为什么排除（约束 C）；
- 报告里每个 evidence id 都能在证据库 verify() 通过（约束 D）；
- cred 类线索不出现明文（约束 F，用带 sk- 的假凭证）；
- secpt run 新目录 → 退出码与终态匹配；secpt resume 空目录 → 1（约束 G）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from pa.adjudicate import finding_id
from pa.infra import evidence
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Adjudication,
    Budget,
    Clue,
    FindingProposal,
    MissionSpec,
)
from pa.report import render_report, save_report
from pa.runtime import run
from pa.store.event import apply_event
from pa.store.view import StoreView
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
    from pa.assets.snapshot import AssetSnapshot

    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])


def _spec(port: int, *, steps: int = 12, evidence_: str = "open") -> MissionSpec:
    return MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1",
                item="端口开放被证实",
                evidence=evidence_,
                target="127.0.0.1",
            )
        ],
        budget=Budget(steps=steps, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{port}"],
    )


def _planner_script(port: int, evidence_: str) -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{port}",
                    "success_criteria": [{"evidence": evidence_}],
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


def _finished_state(tmp_mission, port):
    llm = MockLLM(
        {"planner": [_planner_script(port, "open")], "worker": [_worker_script(port)]}
    )
    return run(tmp_mission, _spec(port), llm=llm, assets=_freeze())


def _inject(state, writer, finding: FindingProposal, verdict: str, reason: str):
    fid = finding_id(finding)
    ev = writer.append(
        "finding.proposed",
        "worker:t-0001",
        {"finding": finding.model_dump(mode="json")},
        f"fp:{fid}",
    )
    if ev is not None:  # 稳定身份下同一提案重复落账会被幂等去重
        apply_event(state, ev)
    adj = Adjudication(finding_id=fid, verdict=verdict, reason=reason)
    ev = writer.append(
        "finding.adjudicated", "engine", adj.model_dump(mode="json"), f"adj:{fid}"
    )
    if ev is not None:
        apply_event(state, ev)
    return fid


def _body_and_appendix(text: str):
    body = text.split("## 发现（confirmed）")[1].split("## 未达成项与原因")[0]
    appendix = text.split("## 附录 A")[1]
    return body, appendix


# ─────────────────────────── 报告（约束 B/C/D/F） ───────────────────────────


def test_render_is_byte_stable(tmp_mission, listener):
    state = _finished_state(tmp_mission, listener)
    view = StoreView(tmp_mission)
    a = render_report(state, view)
    b = render_report(state, view)
    assert a == b
    assert "spent_seconds" not in a  # 墙钟量不入口（确定性）


def test_body_only_confirmed_appendix_holds_rest(tmp_mission, listener):
    state = _finished_state(tmp_mission, listener)
    writer = StoreWriter(tmp_mission)
    e1 = evidence.put(
        tmp_mission,
        json.dumps(
            {"tool": "probe", "exit": 0, "stdout": "open", "stderr": ""}
        ).encode(),
        task_id="t-0001",
    )
    e2 = evidence.put(
        tmp_mission,
        json.dumps(
            {"tool": "probe", "exit": 0, "stdout": "open2", "stderr": ""}
        ).encode(),
        task_id="t-0001",
    )
    _inject(
        state,
        writer,
        FindingProposal(
            claim="已确认端口开放",
            severity="high",
            evidence_refs=[e1, e2],
            reproduce="重跑 probe",
            falsify_if=["连接被拒绝"],
        ),
        "confirmed",
        "证据 2 条、含原始输出、已声明证伪条件",
    )
    _inject(
        state,
        writer,
        FindingProposal(
            claim="疑似弱口令",
            severity="med",
            evidence_refs=[e1],
            reproduce="重试登录",
            falsify_if=["锁定"],
        ),
        "suspected",
        "证据不足：refs=1",
    )
    _inject(
        state,
        writer,
        FindingProposal(
            claim="误报的幽灵端口",
            severity="low",
            evidence_refs=["ev-" + "0" * 16],
            reproduce="无法复现",
            falsify_if=["一切正常"],
        ),
        "false_positive",
        "证据链断裂：1 条引用全部无法核验（verify 失败），排除",
    )

    text = render_report(state, StoreView(tmp_mission))
    body, appendix = _body_and_appendix(text)
    assert "已确认端口开放" in body
    assert "疑似弱口令" not in body  # 约束 C：suspected 不进正文
    assert "误报的幽灵端口" not in body
    assert "疑似弱口令" in appendix
    assert "误报的幽灵端口" in appendix
    assert "为什么排除" in appendix  # false_positive 写清排除理由


def test_overview_verdict_counts_follow_merged_findings(tmp_mission, listener):
    # 稳定 finding_id（claim/reproduce/asset_id）：同 claim 双提案合并为
    # 1 条 findings、裁决幂等只记一次——概述行按合并后 findings 反查裁决
    # 计数，与正文/附录同口径（真跑 do-4a1e630e 的孤儿裁决已消除）。
    state = _finished_state(tmp_mission, listener)
    writer = StoreWriter(tmp_mission)
    e1 = evidence.put(
        tmp_mission,
        json.dumps(
            {"tool": "probe", "exit": 0, "stdout": "git", "stderr": ""}
        ).encode(),
        task_id="t-0001",
    )
    base = {
        "claim": "疑似 /.git 目录泄露",
        "severity": "low",
        "reproduce": "重放 /.git/config 请求",
        "falsify_if": ["返回 404"],
    }
    fid1 = _inject(
        state,
        writer,
        FindingProposal(evidence_refs=[e1], **base),
        "suspected",
        "证据不足：refs=1、无法核验=0",
    )
    fid2 = _inject(
        state,
        writer,
        FindingProposal(evidence_refs=[], **base),
        "suspected",
        "证据不足：refs=0",
    )
    # 稳定身份（claim/reproduce/asset_id）：evidence 演进不再换 id——
    # 裁决始终挂在同一条发现上，不再产生孤儿裁决（真跑 do-4a1e630e 实例）。
    assert fid1 == fid2
    assert len(state.findings) == 1  # 投影合并生效
    assert len(state.adjudications) == 1  # 同 id 裁决幂等，只记一次
    text = render_report(state, StoreView(tmp_mission))
    assert "- 发现：1（confirmed 0、suspected 1、false_positive 0）" in text


def test_every_evidence_id_in_report_verifies(tmp_mission, listener):
    state = _finished_state(tmp_mission, listener)
    writer = StoreWriter(tmp_mission)
    e1 = evidence.put(
        tmp_mission,
        json.dumps(
            {"tool": "probe", "exit": 0, "stdout": "open", "stderr": ""}
        ).encode(),
        task_id="t-0001",
    )
    e2 = evidence.put(
        tmp_mission,
        json.dumps(
            {"tool": "probe", "exit": 0, "stdout": "open2", "stderr": ""}
        ).encode(),
        task_id="t-0001",
    )
    _inject(
        state,
        writer,
        FindingProposal(
            claim="已确认端口开放",
            severity="high",
            evidence_refs=[e1, e2],
            reproduce="重跑 probe",
            falsify_if=["连接被拒绝"],
        ),
        "confirmed",
        "证据 2 条、含原始输出、已声明证伪条件",
    )
    text = render_report(state, StoreView(tmp_mission))
    refs = set(re.findall(r"ev-[0-9a-f]{16}", text))
    assert len(refs) >= 2
    for r in refs:
        assert evidence.verify(tmp_mission, r), r
    # 约束 D：每个 ref 都带 sha256 前 12 位
    assert re.search(r"sha256 前 12 位：[0-9a-f]{12}", text)


def test_cred_clues_are_masked(tmp_mission, listener):
    state = _finished_state(tmp_mission, listener)
    writer = StoreWriter(tmp_mission)
    secret = "sk-fakecred1234567890SECRET"
    ev = writer.append(
        "clue.added",
        "worker:t-0001",
        {
            "clue": Clue(kind="cred", key="api_key", value=secret).model_dump(
                mode="json"
            )
        },
        "clue:cred:1",
    )
    apply_event(state, ev)
    text = render_report(state, StoreView(tmp_mission))
    assert secret not in text  # 明文绝不出现
    assert "sk-f" in text  # 脱敏保留前 4 位


def test_report_has_unmet_section_even_when_all_met(tmp_mission, listener):
    state = _finished_state(tmp_mission, listener)
    text = render_report(state, StoreView(tmp_mission))
    assert "## 未达成项与原因" in text
    assert "无（全部达成）" in text


# ─────────────────────────── 终态自动落盘（b8） ───────────────────────────


def test_run_finished_writes_report(tmp_mission, listener):
    state = _finished_state(tmp_mission, listener)
    report = tmp_mission / "report.md"
    # 真跑正常收口 → run() return 前自动落盘，无需手动 secpt report
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "# secpt 渗透测试报告" in text
    assert f"终止原因：{state.finish_reason}" in text


def test_resume_finished_rewrites_identical_report(tmp_mission, listener):
    _finished_state(tmp_mission, listener)
    text = (tmp_mission / "report.md").read_text(encoding="utf-8")
    # resume 已 finished 的目录：重放后 return 前再次落盘（幂等重写），
    # render_report 确定性保证逐字节相等——这是恢复路径收敛的正确行为
    run(tmp_mission, llm=MockLLM({}), assets=_freeze())
    assert (tmp_mission / "report.md").read_text(encoding="utf-8") == text


def test_save_report_is_idempotent_and_deterministic(tmp_mission, listener):
    state = _finished_state(tmp_mission, listener)
    first = (tmp_mission / "report.md").read_bytes()
    out = save_report(state, tmp_mission)
    assert out == tmp_mission / "report.md"
    assert (tmp_mission / "report.md").read_bytes() == first  # 约束 B：逐字节等价


def test_save_report_degrades_on_failure(tmp_mission, listener, caplog):
    state = _finished_state(tmp_mission, listener)
    bad_root = tmp_mission / "no-such-dir" / "sub"  # 父目录缺失 → 写盘异常
    with caplog.at_level(logging.WARNING, logger="secpt.report"):
        result = save_report(state, bad_root)
    # 静默降级：返回 None + 告警，绝不抛异常打断 run() 的退出路径
    assert result is None
    assert "终态报告落盘失败" in caplog.text


def test_cli_run_fresh_dir_report_not_leftover(tmp_mission, listener):
    # 新目录 run 完自动生成报告；follow.watch 只打印不生成（L211 口径），
    # 这里反向验证：报告确实由 run 写出，而非 watch 之外旧文件残留
    d = tmp_mission / "auto"
    spec = tmp_mission / "spec-auto.json"
    spec.write_text(
        json.dumps(_spec(listener).model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    assert not (d / "report.md").exists()  # run 前不存在
    r = _cli(["run", str(d), "--spec", str(spec)], env_extra=_mock_env(listener))
    assert r.returncode == 0, r.stderr
    assert (d / "report.md").exists()


# ─────────────────────────── CLI 退出码（约束 G） ───────────────────────────


def _cli(args, env_extra=None):
    env = dict(os.environ)
    env.pop("SECPT_LLM_MOCK", None)
    env.pop("SECPT_DIE_AT_STEP", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "pa.cli", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
    )


def _mock_env(port, evidence_="open", worker_args=None):
    item = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": worker_args or {"target": "127.0.0.1", "port": port},
            "expect": "x",
        },
        ensure_ascii=False,
    )
    script = {"planner": [_planner_script(port, evidence_)], "worker": [item]}
    return {"SECPT_LLM_MOCK": json.dumps(script, ensure_ascii=False)}


def test_cli_run_new_dir_exit_code_matches_terminal(tmp_mission, listener):
    # completed → 0
    d = tmp_mission / "m0"
    spec = tmp_mission / "spec.json"
    spec.write_text(
        json.dumps(_spec(listener).model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    r = _cli(["run", str(d), "--spec", str(spec)], env_extra=_mock_env(listener))
    assert r.returncode == 0, r.stderr
    assert "completed" in r.stdout

    # budget_steps → 2
    d2 = tmp_mission / "m2"
    spec2 = tmp_mission / "spec2.json"
    spec2.write_text(
        json.dumps(
            _spec(listener, steps=2, evidence_="never-ever-met").model_dump(
                mode="json"
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    r = _cli(
        ["run", str(d2), "--spec", str(spec2)],
        env_extra=_mock_env(listener, evidence_="never-ever-met"),
    )
    assert r.returncode == 2, r.stderr

    # plateau → 3
    d3 = tmp_mission / "m3"
    spec3 = tmp_mission / "spec3.json"
    spec3.write_text(
        json.dumps(
            _spec(listener, evidence_="never-ever-met").model_dump(mode="json"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    r = _cli(
        ["run", str(d3), "--spec", str(spec3)],
        env_extra=_mock_env(
            listener,
            evidence_="never-ever-met",
            worker_args={"target": "10.255.255.1", "port": 80},
        ),
    )
    assert r.returncode == 3, (r.stdout, r.stderr)


def test_cli_resume_empty_dir_returns_1(tmp_mission):
    empty = tmp_mission / "empty"
    empty.mkdir()
    r = _cli(["resume", str(empty)])
    assert r.returncode == 1
    assert not (empty / "events.jsonl").exists()  # 不许静默新建
