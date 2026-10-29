"""W3-3 失速救场 contract test：Coach(LLMC) 只提案不裁决。

三段分工：
1) Coach 器官纯测（照 Distiller 手式）：propose 形状 / prompt 携带双图与规划 /
   idea 空白 → None / schema 三次耗尽 → E_SCHEMA（协议层语义，共 3 次尝试）；
2) 主循环接线（WF5 失速环）：拉铃 → 事件线落 stall_alarm（不管钱不管救，只报告）→
   名额判定 → LLMC 提案 → audit(actor=llmc) + StallCounter.reset() + 提案文本挤进
   下一步注意力包（吸收一次即用掉）；救场名额每 run 一次，用完再拉铃原样 E_STALL；
3) 可降级器官路径：coach 缺位 / 脚本耗尽 / idea 空白一律降级留痕（structlog
   warning + audit actor=main）后走 stalled 出口，绝不改变收束语义。
"""

from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from secpt.core.loop import PentestLoop
from secpt.errors import LlmError, SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import LlmClient
from secpt.llms.coach import Coach, Proposal
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.ob.events import EventBus
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import NmapTool
from secpt.tools.registry import ToolRegistry

# ---------- 器官纯测：Coach.propose ---------------------------------------------------


def test_propose_returns_proposal_and_prompt_carries_graphs_and_plan() -> None:
    """一次 structured 调用产出 Proposal；prompt 里双图摘要与规划一并在线。"""
    fake = FakeLLM([Proposal(idea="换打 443", reason="22 端口打了五步都没动静")])
    coach = Coach(LlmClient(fake))
    proposal = coach.propose("节点 2（asset=2）；边 3 条", "（规划占位：A/B/C 未接线）")
    assert proposal is not None
    assert proposal.idea == "换打 443"
    assert proposal.reason == "22 端口打了五步都没动静"
    assert fake.call_count == 1  # 只提案不裁决 = 只烧一次结构化调用
    prompt = fake.calls[0][0].content
    assert "节点 2（asset=2）" in prompt  # 双图摘要在场
    assert "规划占位：A/B/C 未接线" in prompt  # 当前规划在场


def test_propose_blank_idea_means_no_advice_returns_none() -> None:
    """idea 空白 = 无话可说：约定 None（提案的可选性由器官层收口）。"""
    fake = FakeLLM([Proposal(idea="   ", reason="无")])
    coach = Coach(LlmClient(fake))
    assert coach.propose("节点 0", "规划占位") is None
    assert fake.call_count == 1


def test_propose_bad_schema_retry_exhausted_raises_e_schema() -> None:
    """schema 带错重试 ≤2（共 3 次尝试）耗尽 → E_SCHEMA：协议层同一契约不豁免。"""
    fake = FakeLLM(["{bad json", "{bad json", "{bad json"])
    coach = Coach(LlmClient(fake))
    with pytest.raises(LlmError, match="反复不合 schema") as info:
        coach.propose("节点 1（asset=1）", "规划占位")
    assert info.value.code == "E_SCHEMA"
    assert fake.call_count == 3


# ---------- 接线共用小工具 -------------------------------------------------------------


def _write_script(tmp_path: Path, body: str) -> Path:
    """假 nmap 二进制：/bin/sh 脚本加可执行位，零网络（照 e2e 手式）。"""
    path = tmp_path / "fake_nmap.sh"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _goal_json(target: str) -> str:
    return json.dumps(
        {
            "target": target,
            "objective": "确认暴露面",
            "success_criteria": "拿到一次 exit=0 小票",
        },
        ensure_ascii=False,
    )


def _step_json(
    thought: str,
    calls: list[dict[str, object]],
    *,
    done: bool,
    plan_done: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "thought": thought,
            "plan_updates": [],
            "plan_done": plan_done or [],
            "tool_calls": calls,
            "done": done,
        },
        ensure_ascii=False,
    )


def _make_db() -> sqlite3.Connection:
    return migrate_store.connect(":memory:")


def _recorder() -> tuple[EventBus, list[tuple[str, Mapping[str, object]]]]:
    events: list[tuple[str, Mapping[str, object]]] = []
    bus = EventBus()
    bus.subscribe(lambda name, payload: events.append((name, payload)))
    return bus, events


def _phantom_step(n: int) -> str:
    """必败的一步：点名未登记工具 → 回执 exit_code=None → 失速计 +1。"""
    return _step_json(
        f"走老路第 {n} 步",
        calls=[{"name": "phantom_tool", "args": {}}],
        done=False,
    )


def _audit_what(conn: sqlite3.Connection, actor: str) -> list[str]:
    rows = conn.execute("SELECT what FROM audit_log WHERE actor = ?", (actor,)).fetchall()
    return [str(r["what"]) for r in rows]


# ---------- 接线：救场被吸收 → 换路成功 → normal 收束 --------------------------------


def test_rescue_absorbed_run_recovers_to_normal(tmp_path: Path) -> None:
    """拉铃 → stall_alarm 落铃 → LLMC 提案（audit llmc + reset）→ 下一步注意力包
    携带提案文本吸收一次 → 换路成功 → normal 收束（验收③主场景）。"""
    nmap = _write_script(tmp_path, 'echo "443/tcp open nginx"')
    registry = ToolRegistry([NmapTool(binary=str(nmap), raw_dir=str(tmp_path / "raw"))])
    fake = FakeLLM(
        [
            _goal_json("10.0.0.5"),
            *[_phantom_step(n) for n in range(1, 6)],  # 连败 5 步 → 拉铃
            Proposal(idea="换打 443", reason="连续 5 步未知工具无进展"),
            _step_json(
                "听教练的，换打 443",
                calls=[{"name": "nmap", "args": {"target": "10.0.0.5", "ports": "443"}}],
                done=False,
            ),
            _step_json("收工", calls=[], done=True, plan_done=["换打 443"]),
        ]
    )
    conn = _make_db()
    bus, events = _recorder()
    client = LlmClient(fake)
    loop = PentestLoop(
        conn,
        MainLlm(client),
        registry,
        bus=bus,
        exit_checker=ExitChecklist(),
        coach=Coach(client),
    )
    loop.run("救场演示 10.0.0.5")

    assert fake.call_count == 9  # 进场 + 必败 5 + 教练 1 + 换路 1 + 收工 1

    # 吸收断言：救场后第一个主 step 的注意力包 user 消息携带提案文本（一次即用掉）。
    absorb = fake.calls[7][1].content
    assert "教练提案待吸收（WF5 救场，只提案不裁决）" in absorb
    assert "换打 443" in absorb

    # 事件序：五连败（含小票）→ stall_alarm → 换路步 → 收工步 → exit_passed → run_ended。
    names = [name for name, _ in events]
    assert names == (
        ["run_step_started", "ticket_appended", "graph_changed"] * 5
        + [
            "stall_alarm",
            "run_step_started",
            "ticket_appended",
            "graph_changed",
            "run_step_started",
            "graph_changed",
            "exit_passed",
            "run_ended",
        ]
    )

    # 账本事实：audit llmc 恰 1 行（提案落账），出场三验过 → normal 收束。
    llmc = _audit_what(conn, "llmc")
    assert len(llmc) == 1
    assert "教练提案" in llmc[0] and "换打 443" in llmc[0]
    task = ledger.latest_task(conn)
    assert task.exit_kind == "normal"


# ---------- 接线：名额用完 → 第二次拉铃原样 E_STALL ----------------------------------


def test_second_ring_after_rescue_spent_raises_and_stalls(tmp_path: Path) -> None:
    """救场名额每 run 一次（WF5 失败路径）：第二次拉铃不再有教练，走 E_STALL stalled。"""
    registry = ToolRegistry([])  # 整场没有可用工具：10 连败
    fake = FakeLLM(
        [
            _goal_json("10.0.0.5"),
            *[_phantom_step(n) for n in range(1, 6)],  # 第一波连败 5 → 拉铃，教练救场
            Proposal(idea="换打 443", reason="第一次救场"),
            *[_phantom_step(n) for n in range(6, 11)],  # 换打法后仍败 5 → 二次拉铃
        ]
    )
    conn = _make_db()
    bus, events = _recorder()
    client = LlmClient(fake)
    loop = PentestLoop(
        conn,
        MainLlm(client),
        registry,
        bus=bus,
        exit_checker=ExitChecklist(),
        coach=Coach(client),
    )
    with pytest.raises(SecptError, match="失速计拉铃") as info:
        loop.run("名额演示 10.0.0.5")
    assert info.value.code == "E_STALL"

    assert fake.call_count == 12  # 进场 + 10 败 + 教练 1（第二次拉铃不再烧教练）
    assert fake.remaining == 0
    assert [name for name, _ in events] == (
        ["run_step_started", "ticket_appended", "graph_changed"] * 5
        + ["stall_alarm"]  # 第一次拉铃：铃照落，救场提案随后被吸收
        + ["run_step_started", "ticket_appended", "graph_changed"] * 5
        + ["stall_alarm", "run_ended"]  # 第二次拉铃：名额已用，raise E_STALL
    )
    assert len(_audit_what(conn, "llmc")) == 1  # 教练只发言过一次
    task = ledger.latest_task(conn)
    assert task.exit_kind == "stalled"


# ---------- 兜底：coach 缺位 / 教练无话 / 教练急病 ------------------------------------


def test_coach_absent_keeps_m1_semantics(tmp_path: Path) -> None:
    """coach=None（M1 老装配）：铃照落（事件线只管报告），出口语义与 M1 完全一致。"""
    registry = ToolRegistry([])
    fake = FakeLLM([_goal_json("10.0.0.5"), *[_phantom_step(n) for n in range(1, 6)]])
    conn = _make_db()
    bus, events = _recorder()
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        registry,
        bus=bus,
        exit_checker=ExitChecklist(),
    )
    with pytest.raises(SecptError, match="失速计拉铃") as info:
        loop.run("无教练演示 10.0.0.5")
    assert info.value.code == "E_STALL"
    assert [name for name, _ in events] == (
        ["run_step_started", "ticket_appended", "graph_changed"] * 5 + ["stall_alarm", "run_ended"]
    )
    assert _audit_what(conn, "llmc") == []
    assert ledger.latest_task(conn).exit_kind == "stalled"


def test_coach_script_exhausted_degrades_then_stalls(tmp_path: Path) -> None:
    """教练急病（脚本耗尽 = LlmError）：可降级器官——warning + audit(actor=main)
    留痕，随即按原语义 E_STALL 走 stalled，绝不悬死不遮错。"""
    registry = ToolRegistry([])
    fake = FakeLLM([_goal_json("10.0.0.5"), *[_phantom_step(n) for n in range(1, 6)]])
    conn = _make_db()
    bus, _ = _recorder()
    client = LlmClient(fake)
    loop = PentestLoop(
        conn,
        MainLlm(client),
        registry,
        bus=bus,
        exit_checker=ExitChecklist(),
        coach=Coach(client),
    )
    with pytest.raises(SecptError, match="失速计拉铃") as info:
        loop.run("教练掉线演示 10.0.0.5")
    assert info.value.code == "E_STALL"
    assert fake.call_count == 7 and fake.remaining == 0  # 教练那通真实发生且耗尽
    degraded = [what for what in _audit_what(conn, "main") if "教练提案降级" in what]
    assert len(degraded) == 1
    assert _audit_what(conn, "llmc") == []
    assert ledger.latest_task(conn).exit_kind == "stalled"


def test_coach_blank_proposal_degrades_then_stalls(tmp_path: Path) -> None:
    """教练无话（idea 空白 → None）：同一降级路径——留痕后 E_STALL，不静默吞掉。"""
    registry = ToolRegistry([])
    fake = FakeLLM(
        [
            _goal_json("10.0.0.5"),
            *[_phantom_step(n) for n in range(1, 6)],
            Proposal(idea="  ", reason="没辙"),
        ]
    )
    conn = _make_db()
    bus, _ = _recorder()
    client = LlmClient(fake)
    loop = PentestLoop(
        conn,
        MainLlm(client),
        registry,
        bus=bus,
        exit_checker=ExitChecklist(),
        coach=Coach(client),
    )
    with pytest.raises(SecptError, match="失速计拉铃") as info:
        loop.run("教练没辙演示 10.0.0.5")
    assert info.value.code == "E_STALL"
    assert fake.call_count == 7
    assert [what for what in _audit_what(conn, "main") if "教练提案降级" in what]
    assert _audit_what(conn, "llmc") == []  # None 不是提案，不落 llmc 账
