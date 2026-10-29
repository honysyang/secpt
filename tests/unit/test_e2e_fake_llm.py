"""W2-3 FakeLLM 端到端（双向）：真实装配下正常/异常多方向全走通。

与面上单测的分工：这里不再 Fake dispatcher / 手铺小票假设，而是真实
ToolRegistry（nmap→假二进制，零网络）+ 真实 ExitChecklist + 真实 LlmClient
协议链，验证整条流水线在 FakeLLM 演示下两头都通：
- 正常方向：进场三问 → nmap 真调度落小票（raw_ref 为实际文件）→ done →
  出场三验过 → normal 收束 → 终场报告（结构化 + Markdown）；
- 异常方向：步阶段连吐坏 JSON → 协议层 schema 带错重试链（≤2，共 3 次尝试）
  耗尽 → E_SCHEMA → voided step 落账 → abnormal 出口 → 半场报告
  （claimed 悬置如实上数）；
- 失速方向：nmap 缺二进制连败 5 步 → 失速计拉铃 E_STALL → stalled 出口（§9 落账）。
"""

from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from secpt.core.loop import JudgeSweep, PentestLoop
from secpt.errors import LlmError, SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.finalize.experience import Distiller
from secpt.finalize.reporter import Reporter, render_markdown
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.ob.events import EventBus
from secpt.store import experience, ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import NmapTool
from secpt.tools.registry import ToolRegistry


def _write_script(tmp_path: Path, body: str) -> Path:
    """假 nmap 二进制：/bin/sh 脚本加可执行位，零网络。"""
    path = tmp_path / "fake_nmap.sh"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _step_json(thought: str, *, calls: list[dict[str, object]], done: bool) -> str:
    """合法 StepOutput JSON（协议层照常解析，provider 无感）。"""
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": calls, "done": done},
        ensure_ascii=False,
    )


def _summary_json(text: str) -> str:
    """合规报告摘要 JSON（LLMA 的 structured 输出契约）。"""
    return json.dumps({"summary": text}, ensure_ascii=False)


def _goal_json(target: str) -> str:
    """合规进场三问 JSON（Goal 的 structured 输出契约，多个方向共用）。"""
    return json.dumps(
        {
            "target": target,
            "objective": "确认 ssh 暴露面",
            "success_criteria": "小票里出现 ssh banner",
        },
        ensure_ascii=False,
    )


def _make_db() -> sqlite3.Connection:
    return migrate_store.connect(":memory:")


def _recorder() -> tuple[EventBus, list[tuple[str, Mapping[str, object]]]]:
    """事件记录器：订阅回调直接攒列表，测试断言事件序与载荷。"""
    events: list[tuple[str, Mapping[str, object]]] = []
    bus = EventBus()
    bus.subscribe(lambda name, payload: events.append((name, payload)))
    return bus, events


def _make_loop(
    conn: sqlite3.Connection,
    fake: FakeLLM,
    registry: ToolRegistry,
) -> tuple[PentestLoop, list[tuple[str, Mapping[str, object]]]]:
    """真实装配：MainLlm→LlmClient→FakeLLM + 真 registry + 真 ExitChecklist。"""
    bus, events = _recorder()
    loop = PentestLoop(
        conn, MainLlm(LlmClient(fake)), registry, bus=bus, exit_checker=ExitChecklist()
    )
    return loop, events


# ---------- 方向一：正常——真注册表全流程到终场报告 ---------------------------------


def test_normal_direction_full_pass(tmp_path: Path) -> None:
    nmap = _write_script(tmp_path, 'echo "22/tcp open ssh"')
    raw_dir = tmp_path / "raw"
    registry = ToolRegistry([NmapTool(binary=str(nmap), raw_dir=str(raw_dir))])
    fake = FakeLLM(
        [
            json.dumps(
                {
                    "target": "10.0.0.5",
                    "objective": "确认 ssh 暴露面",
                    "success_criteria": "小票里出现 ssh banner",
                },
                ensure_ascii=False,
            ),
            _step_json(
                "摸排 22",
                calls=[{"name": "nmap", "args": {"target": "10.0.0.5", "ports": "22"}}],
                done=False,
            ),
            _step_json("收工", calls=[], done=True),
            _summary_json("M1 e2e：nmap 摸扫一轮，出口三验通过。"),
        ]
    )
    conn = _make_db()
    loop, events = _make_loop(conn, fake, registry)
    result = loop.run("全流程演示 10.0.0.5")

    assert result.finished_naturally is True
    assert result.exit_kind == "normal"
    assert result.steps_taken == 2
    assert fake.call_count == 3  # 进场 1 次 + 两步各 1 次，无重试

    tickets = conn.execute("SELECT * FROM tickets").fetchall()
    assert len(tickets) == 1
    assert tickets[0]["exit_code"] == 0
    assert tickets[0]["raw_ref"] != ""
    assert Path(tickets[0]["raw_ref"]).exists()  # 诚实锚点：raw 指针指向真实文件
    assert "22/tcp open ssh" in Path(tickets[0]["raw_ref"]).read_text(encoding="utf-8")
    run = conn.execute("SELECT status, step_count FROM runs").fetchone()
    assert run["status"] == "ended"
    assert run["step_count"] == 2
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 2
    task = ledger.get_task(conn, result.task_id)
    assert task.exit_kind == "normal"
    names = [name for name, _ in events]
    assert names == [
        "run_step_started",
        "ticket_appended",
        "graph_changed",
        "run_step_started",
        "graph_changed",
        "exit_passed",
        "run_ended",
    ]

    rep = Reporter(LlmClient(fake)).generate(conn, result.task_id)
    assert fake.remaining == 0  # 脚本正好耗尽：第 4 条 = 报告摘要，与流程严丝合缝
    md = render_markdown(rep)
    assert rep.half_court is False and rep.exit_kind == "normal"
    assert rep.findings == []
    assert rep.failing_counts == {"exit_checklist": 0, "abnormal": 0}
    assert md.startswith("# 终场报告") and "（无 verified findings，如实照录）" in md
    assert "出口三验通过" in md


# ---------- 方向二：异常——schema 重试链耗尽 → abnormal → 半场报告 -------------------


def test_abnormal_direction_schema_retry_exhausted_then_half_report(tmp_path: Path) -> None:
    conn = _make_db()
    # 铺账本现场：task + run（三问落 config）+ 一条 claimed 悬置 finding → resume 续跑。
    task = ledger.start_task(conn, "坏 output 主机", goal="10.0.0.5")
    ledger.start_run(
        conn, task.id, config={"objective": "确认暴露面", "success_criteria": "banner"}
    )
    ledger.append_finding(conn, task.id, "悬置发现", "待裁决", "")
    fake = FakeLLM(["{bad json", "{bad json", "{bad json"])
    loop, events = _make_loop(conn, fake, ToolRegistry([]))

    # 协议层重试链：1+2 次重试共 3 次尝试全坏 → E_SCHEMA（库层语义）。
    with pytest.raises(LlmError, match="反复不合 schema") as info:
        loop.resume(task.id)
    assert info.value.code == "E_SCHEMA"
    assert fake.call_count == 3
    assert fake.remaining == 0

    # 异常出口落账齐全：voided step + run ended + task abnormal，绝不悬死。
    steps = conn.execute("SELECT step_no, status FROM steps").fetchall()
    assert [(row["step_no"], row["status"]) for row in steps] == [(1, "voided")]
    run = conn.execute("SELECT status, step_count FROM runs").fetchone()
    assert run["status"] == "ended" and run["step_count"] == 0
    assert ledger.get_task(conn, task.id).exit_kind == "abnormal"
    assert [name for name, _ in events] == ["run_step_started", "run_ended"]

    # 半场报告（main.py 兜底同款手式）：claimed 悬置 + voided 步如实上数
    # （M5-19 起"步骤是否都完成"也是一条出场验，voided 步因此计 2 条）。
    reporter = Reporter(LlmClient(FakeLLM([_summary_json("坏 JSON 三连，半场收束。")])))
    rep = reporter.generate(conn, task.id, half_court=True)
    md = render_markdown(rep)
    assert rep.half_court is True and rep.exit_kind == "abnormal"
    assert rep.findings == []  # claimed ≠ verified：claims 进不了报告
    assert rep.failing_counts == {"exit_checklist": 2, "abnormal": 1}
    assert md.startswith("# 半场报告") and "（无 verified findings，如实照录）" in md
    assert "failing_counts={'exit_checklist': 2, 'abnormal': 1}" in md
    assert "半场收束" in md


# ---------- 方向三：失速——nmap 缺二进制连败 5 步 → E_STALL → stalled ----------------


def test_stall_direction_e_stall_exit(tmp_path: Path) -> None:
    registry = ToolRegistry([NmapTool(binary="/nonexistent-nmap-m1", raw_dir=str(tmp_path))])
    fake = FakeLLM(
        [
            json.dumps(
                {
                    "target": "10.0.0.5",
                    "objective": "确认暴露面",
                    "success_criteria": "小票回执一律 exit=0",
                },
                ensure_ascii=False,
            ),
            *[
                _step_json(
                    f"第 {n} 步",
                    calls=[{"name": "nmap", "args": {"target": "10.0.0.5"}}],
                    done=False,
                )
                for n in range(1, 5)
            ],
        ]
    )
    conn = _make_db()
    loop, events = _make_loop(conn, fake, registry)

    # 同参数重复 4 次 = 失速计 repeat_calls=4 拉铃（M5-31 第三口径比连败 N=5 先响）：
    # E_STALL（SecptError 家族）原样上抛给 main 兜底。
    with pytest.raises(SecptError, match="失速计拉铃") as info:
        loop.run("失速演示 10.0.0.5")
    assert info.value.code == "E_STALL"
    assert fake.call_count == 5  # 进场 1 次 + 四步各 1 次，无 schema 重试
    assert fake.remaining == 0

    # 四步账都在：小票失败回执（exit_code=NULL、raw_ref 空）+ stalled 出口。
    tickets = conn.execute("SELECT exit_code, raw_ref FROM tickets").fetchall()
    assert len(tickets) == 4
    assert all(row["exit_code"] is None and row["raw_ref"] == "" for row in tickets)
    run = conn.execute("SELECT status, step_count FROM runs").fetchone()
    assert run["status"] == "ended" and run["step_count"] == 4
    task = ledger.latest_task(conn)
    assert task.exit_kind == "stalled"
    names = [name for name, _ in events]
    # 失速环事件契约扩展（W3-3 记 §65 + M4-6 graph_changed）：拉铃必落
    # stall_alarm（事件线只管报告，本方向无 coach，铃后仍按 M1 语义 E_STALL）；
    # 每步事务提交后另有 graph_changed（双图写入只报告，R3 红线不破）。
    assert names == (
        ["run_step_started", "ticket_appended", "graph_changed"] * 4 + ["stall_alarm", "run_ended"]
    )


# ---------- 方向四：经验沉淀 + 二轮回放（W3-1，验收②基础版） -------------------------


def test_things_learned_then_replayed_across_runs(tmp_path: Path) -> None:
    """第一轮 run 收尾经 Distiller 落 experience（audit llmb）；M3-4 收紧（拍板④）下
    claimed 判票前不进回放位，promote_claimed 升 verified 后第二轮包正面出条带命令。"""
    nmap = _write_script(tmp_path, 'echo "22/tcp open ssh"')
    registry = ToolRegistry([NmapTool(binary=str(nmap), raw_dir=str(tmp_path / "raw"))])
    conn = _make_db()

    distill_draft = json.dumps(
        {"entries": [{"idx": 0, "summary": "22 端口扫描可达", "command": "nmap -p 22"}]},
        ensure_ascii=False,
    )
    fake1 = FakeLLM(
        [
            _goal_json("10.0.0.5"),
            _step_json(
                "摸排 22",
                calls=[{"name": "nmap", "args": {"target": "10.0.0.5", "ports": "22"}}],
                done=False,
            ),
            _step_json("收工", calls=[], done=True),
            distill_draft,  # 第 4 通 = Distiller 蒸馏材料
        ]
    )
    client1 = LlmClient(fake1)
    bus1, events1 = _recorder()
    loop1 = PentestLoop(
        conn,
        MainLlm(client1),
        registry,
        bus=bus1,
        exit_checker=ExitChecklist(),
        distiller=Distiller(client1),
        experience_env="lab",
    )
    result1 = loop1.run("经验沉淀演示 10.0.0.5")
    assert result1.exit_kind == "normal"
    assert fake1.call_count == 4  # 进场 1 + 两步 1 + 蒸馏 1（成功小票烧了一次 LLM）
    rows = conn.execute("SELECT * FROM experience").fetchall()
    assert len(rows) == 1
    assert rows[0]["verify_state"] == "claimed"  # 判票在 M3，claims 如实入库
    audits = conn.execute("SELECT actor, what FROM audit_log WHERE actor = 'llmb'").fetchall()
    assert len(audits) == 2  # 落账 1 行 + 蒸馏收束 1 行

    # 第二轮同环境 run：Barrett 收紧（拍板④）——判票前 claimed 不进回放位；
    # 经票盒写口 promote_claimed 升 verified 后回放位出现正典条目（命令原样带出）。
    assert experience.fetch_replay(conn, "lab") == []
    exp_rows = conn.execute("SELECT task_id, raw_ref FROM experience").fetchall()
    assert len(exp_rows) == 1
    promoted = experience.promote_claimed(
        conn, exp_rows[0]["task_id"], "lab", exp_rows[0]["raw_ref"]
    )
    assert promoted and promoted[0].verify_state == "verified"

    fake2 = FakeLLM([_goal_json("10.0.0.5"), _step_json("收工", calls=[], done=True)])
    bus2, _ = _recorder()
    loop2 = PentestLoop(
        conn,
        MainLlm(LlmClient(fake2)),
        registry,
        bus=bus2,
        exit_checker=ExitChecklist(),
        distiller=Distiller(LlmClient(fake2)),
        experience_env="lab",
    )
    result2 = loop2.run("同环境二轮 10.0.0.5")
    assert result2.finished_naturally is True
    pack_text = fake2.calls[1][1].content  # 第 1 通是进场三问，第 2 通才是注意力包
    assert "## 可用经验" in pack_text
    assert "[verified] 22 端口扫描可达" in pack_text
    assert "（命令：nmap -p 22）" in pack_text


# ---------- 方向五：裁判 R0/R4 上线（W3-2，验收①） ------------------------------------


def test_judge_full_pass(tmp_path: Path) -> None:
    """declare 受理 → 声明落账 → sweep 即时裁决：bad claim 被 R0 判 rejected，
    命中 ground_truth 的 claim 变 verified(oracle)，报告只收 verified。"""
    from functools import partial
    from typing import cast

    from secpt.judge import GroundTruth, JudgeProfile, note_for, sweep
    from secpt.tools.adapters import DeclareFindingTool

    gold = tmp_path / "gold.txt"
    gold.write_text("22/tcp open ssh  OpenSSH 9.2", encoding="utf-8")
    registry = ToolRegistry([DeclareFindingTool()])  # 裁判演示不需要 nmap
    from secpt.finalize.exit_checklist import ExitChecklist
    from secpt.finalize.reporter import Reporter
    from secpt.llms.main_llm import MainLlm

    conn = _make_db()
    fake = FakeLLM(
        [
            _goal_json("10.0.0.5"),
            _step_json(
                "两张申报单：一真一假",
                calls=[
                    {
                        "name": "declare_finding",
                        "args": {
                            "title": "22/tcp open on target",
                            "detail": "OpenSSH 9.2 开放 22",
                            "evidence_ref": str(gold),
                        },
                    },
                    {
                        "name": "declare_finding",
                        "args": {
                            "title": "灵魂漏洞",
                            "detail": "凭空想出来的 RCE",
                            "evidence_ref": str(tmp_path / "phantom.txt"),
                        },
                    },
                ],
                done=False,
            ),
            _step_json("收工", calls=[], done=True),
        ]
    )
    # profile 装配已有单测（TestProfile），e2e 手铺启用的 JudgeProfile（oracle 一条）。
    prof = JudgeProfile(
        enabled=True,
        ground_truth=(GroundTruth(id="GT-22", pattern="22/tcp open"),),
        source="e2e",
    )
    sweep_fn = partial(sweep, profile=prof)
    bus, events = _recorder()
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        registry,
        bus=bus,
        exit_checker=ExitChecklist(),
        judge_sweep=cast(JudgeSweep, sweep_fn),
        judge_note=note_for(prof),
    )
    result = loop.run("裁判演示 10.0.0.5")
    assert result.exit_kind == "normal"
    assert fake.call_count == 3  # 进场 + 两步（报告在测试侧用独立 FakeLLM）

    # 账本事实：good 被认 / bad 被零 / 无悬置；audit 双痕（装配注记 + 裁决各行）。
    rows = conn.execute("SELECT * FROM findings ORDER BY rowid").fetchall()
    assert len(rows) == 2
    assert rows[0]["verify_state"] == "verified" and rows[0]["method"] == "oracle"
    assert "22/tcp open" in rows[0]["title"]
    assert rows[1]["verify_state"] == "rejected" and rows[1]["method"] is None
    audits_main = conn.execute("SELECT what FROM audit_log WHERE actor = 'main'").fetchall()
    assert any("裁判装配" in str(r["what"]) for r in audits_main)
    assert any("裁判收口 2 条" in str(r["what"]) for r in audits_main)
    judge_lines = conn.execute("SELECT what FROM audit_log WHERE actor = 'judge'").fetchall()
    assert len(judge_lines) == 2

    # 报告只收 verified：bad claim 不在终场清单里。
    rep = Reporter(LlmClient(FakeLLM([_summary_json("终场摘要。")]))).generate(conn, result.task_id)
    assert [f.title for f in rep.findings] == ["22/tcp open on target"]
    assert [name for name, _ in events][-1] == "run_ended"
