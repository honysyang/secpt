"""M3-5 worker 单元测试（先测试）：步循环骨架、申报写口、诚实降级。

拍板③（§71 前口径）：worker 是独立 LLM 器官——per-thread 结构化步循环
（structured(StepOutput)）；declare_finding 受理小票（exit_code==0 且
raw_ref 非空）才经 ledger.append_finding 直写黑板 claimed；任何异常都降级
成 MissionResult(ok=False, finished=False) 并落 audit（actor=workerA），
绝不静默吞错。findings 写口只经 store/*——worker 不碰 tickets（那是
主循环 step_transaction 的收口权）。

测试主战场 = FakeLLM contract test + :memory: 账本 fixture；每个用例单线程，
不触碰 FakeLLM 非线程安全的边界（并发归 test_mission_board 的板级测试）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Final

from secpt.core.mission_board import Mission
from secpt.errors import LlmError
from secpt.llms.client import LlmClient
from secpt.llms.coach import Coach, Proposal
from secpt.llms.fake import FakeLLM
from secpt.llms.worker import Worker
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import DeclareFindingTool
from secpt.tools.registry import ToolRegistry

WORKER_ACTOR: Final[str] = "workerA"


def _step(thought: str, *, calls: list[dict[str, object]] | None = None, done: bool = False) -> str:
    """构造一步 StepOutput 脚本（_worker 步循环 structured 的应答形状）。"""
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": calls or [], "done": done},
        ensure_ascii=False,
    )


def _declare_call(title: str, detail: str, evidence_ref: str) -> dict[str, object]:
    """declare_finding 的 tool_call 项（args 三参全 str）。"""
    return {
        "name": "declare_finding",
        "args": {"title": title, "detail": detail, "evidence_ref": evidence_ref},
    }


def _assemble(
    script: list[object], mission_kwargs: dict[str, object] | None = None
) -> tuple[Worker, Mission, FakeLLM, sqlite3.Connection]:
    """便捷装配：内存库开 task → Mission 带真锚 → 单线程 worker 起跑。

    返回 conn 供断言直读（findings/audit 查询）——实现内部属性名不进契约。
    """
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, input_text="worker 测试进场", goal="摸扫")
    # M5-25：worker 的小票要挂 run 锚（真实流程里 agent_run 由主循环派发，run 必然在）
    ledger.start_run(conn, task.id, config={"objective": "o", "success_criteria": "s"})
    script_items: list[object] = list(script)
    fake = FakeLLM(script_items)
    client = LlmClient(fake)
    worker = Worker(
        llm=client,
        dispatcher=ToolRegistry([DeclareFindingTool()]),
        conn=conn,
        coach=Coach(client),  # 门槛为 0 时不会真调用（老语义守恒）
    )
    kwargs: dict[str, object] = {"task_id": task.id, "goal": "领单干活"}
    kwargs.update(mission_kwargs or {})
    mission = Mission(persona="worker", **kwargs)  # type: ignore[arg-type]
    return worker, mission, fake, conn


def test_happy_path_declares_finding(tmp_path: Path) -> None:
    """两步走：步1申报受理 → 步2收工；findings 落账 claimed 恰一条。"""
    evidence = tmp_path / "ev.txt"
    evidence.write_text("客观证据\n", encoding="utf-8")
    script = [
        _step("申报", calls=[_declare_call("w 发现", "依据在档", str(evidence))]),
        _step("收工", done=True),
    ]
    worker, mission, _fake, conn = _assemble(script, {"step_budget": 8})
    result = worker.run(mission)

    assert (result.ok, result.finished, result.steps_taken) == (True, True, 2)
    assert result.findings_count == 1
    rows: list[tuple[object, ...]] = [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT verify_state, title, actor FROM findings WHERE task_id=?",
            (mission.task_id,),
        ).fetchall()
    ]
    assert rows == [("claimed", "w 发现", WORKER_ACTOR)]  # M5-11：申报者身份如实落账
    node_keys = [
        row["key"]
        for row in conn.execute("SELECT key FROM graph_nodes WHERE kind='finding'").fetchall()
    ]
    assert len(node_keys) == 1 and node_keys[0].startswith("finding-")  # M5-15 证据图


def test_budget_exhausted_honest_unfinished() -> None:
    """预算告罄：步数吃满即止——ok=True（没出错）finished=False（没干完）。"""
    script = [_step(f"踏步{k}") for k in range(3)]  # 3 步脚本 > 预算 2
    worker, mission, fake, _conn = _assemble(script, {"step_budget": 2})
    result = worker.run(mission)

    assert (result.ok, result.finished, result.steps_taken) == (True, False, 2)
    assert "预算" in result.summary
    assert fake.remaining == 1  # 第 3 步脚本没被消费


def test_unknown_tool_turns_receipt_and_continues() -> None:
    """点名未授权工具：E_SCHEMA_TOOL 小票不炸不 continue-break，走到收工。"""
    script = [
        _step("试错", calls=[{"name": "nmap", "args": {}}]),
        _step("收工", done=True),
    ]
    worker, mission, _fake, _conn = _assemble(script)
    result = worker.run(mission)

    assert (result.ok, result.finished, result.findings_count) == (True, True, 0)
    assert result.steps_taken == 2


def test_llm_failure_degrades_to_honest_result() -> None:
    """LLM 器官认知失败：降级收场不崩 task——ok=False + audit 留眼。"""
    script = [LlmError("炸了")]
    worker, mission, _fake, conn = _assemble(script)
    result = worker.run(mission)

    assert (result.ok, result.finished) == (False, False)
    assert "炸了" in result.summary  # 不吞关键错误：原因必须可见
    audit_rows = conn.execute(
        "SELECT what FROM audit_log WHERE task_id=? AND actor=?",
        (mission.task_id, WORKER_ACTOR),
    ).fetchall()
    assert len(audit_rows) >= 1


def test_worker_audit_actor_is_always_workerA() -> None:
    """actor 分层（蓝本 §4 audit 器官）：worker 全体审计行恒 actor=workerA。"""
    script = [_step("收工", done=True)]
    worker, mission, _fake, conn = _assemble(script)
    worker.run(mission)

    actors = conn.execute(
        "SELECT DISTINCT actor FROM audit_log WHERE task_id=?", (mission.task_id,)
    ).fetchall()
    assert [a[0] for a in actors] == [WORKER_ACTOR]
    whats = [
        row[0]
        for row in conn.execute(
            "SELECT what FROM audit_log WHERE task_id=?", (mission.task_id,)
        ).fetchall()
    ]
    assert any("领单" in w for w in whats)  # 领单痕在案


def test_rejected_declaration_writes_no_finding() -> None:
    """空 evidence_ref 申报：E_TOOL_FAIL 不受理 → findings_count=0 且继续走完。"""
    script = [
        _step("申报", calls=[_declare_call("无凭申报", "空", "")]),
        _step("收工", done=True),
    ]
    worker, mission, _fake, conn = _assemble(script)
    result = worker.run(mission)

    assert (result.ok, result.finished, result.steps_taken) == (True, True, 2)
    assert result.findings_count == 0
    count = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE task_id=?", (mission.task_id,)
    ).fetchone()[0]
    assert count == 0


# ---------- M5-13 教练接入（何时 LLMC 教练员接入） ------------------------------


def _phantoms() -> dict[str, object]:
    """必败调用：未登记工具 → 回执 exit_code=None。"""
    return {"name": "phantom", "args": {}}


def test_worker_coach_enters_after_failure_threshold() -> None:
    """连续失败达门槛 → 教练介入一次，提示进下一步消息，audit 可证。"""
    script = [
        _step("老路", calls=[_phantoms()]),
        Proposal(idea="换打 443", reason="连续失败"),
        _step("听教练的", done=True),
    ]
    worker, mission, fake, conn = _assemble(script, {"coach_after_failures": 1})
    result = worker.run(mission)

    assert result.ok and result.finished
    assert fake.call_count == 3  # 两步 + 教练一次
    assert "教练提示（一次性）：换打 443" in fake.calls[2][1].content
    whats = [
        row["what"]
        for row in conn.execute(
            "SELECT what FROM audit_log WHERE task_id=? AND actor=?",
            (mission.task_id, WORKER_ACTOR),
        ).fetchall()
    ]
    assert any("教练接入（只提案不裁决）" in what for what in whats)


def test_worker_coach_not_called_when_threshold_zero() -> None:
    """门槛 0 = 不接入（M3-5 老语义）：脚本按步数精确耗尽，没有额外教练调用。"""
    script = [
        _step("老路1", calls=[_phantoms()]),
        _step("老路2", calls=[_phantoms()]),
        _step("收工", done=True),
    ]
    worker, mission, fake, conn = _assemble(script)
    worker.run(mission)

    assert fake.remaining == 0 and fake.call_count == 3
    whats = [
        row["what"]
        for row in conn.execute(
            "SELECT what FROM audit_log WHERE task_id=? AND actor=?",
            (mission.task_id, WORKER_ACTOR),
        ).fetchall()
    ]
    assert not any("教练接入" in what for what in whats)


def test_worker_coach_failure_degrades_without_killing_mission() -> None:
    """教练掉线/无话可说：audit 留痕，worker 照常走完（降级不改结论）。"""
    script = [
        _step("老路", calls=[_phantoms()]),
        LlmError("教练炸了"),
        _step("自己收工", done=True),
    ]
    worker, mission, fake, conn = _assemble(script, {"coach_after_failures": 1})
    result = worker.run(mission)

    assert result.ok and result.finished  # 教练失败不拖垮工单
    assert "教练提示" not in fake.calls[2][1].content
    whats = [
        row["what"]
        for row in conn.execute(
            "SELECT what FROM audit_log WHERE task_id=? AND actor=?",
            (mission.task_id, WORKER_ACTOR),
        ).fetchall()
    ]
    assert any("教练接入降级" in what for what in whats)


def test_mission_coach_threshold_defaults_to_zero() -> None:
    assert Mission(goal="g").coach_after_failures == 0


def test_worker_records_tickets_for_tool_calls(tmp_path: Path) -> None:
    """M5-25：worker 的工具动作也要落小票，否则 R1 找不到票面证据源。"""
    evidence = tmp_path / "ev.txt"
    evidence.write_text("客观证据\n", encoding="utf-8")
    script = [
        _step("申报", calls=[_declare_call("w 发现", "依据在档", str(evidence))]),
        _step("收工", done=True),
    ]
    worker, mission, _fake, conn = _assemble(script)
    worker.run(mission)

    rows = conn.execute(
        "SELECT tool_name, exit_code, raw_ref, step_id FROM tickets WHERE task_id=?",
        (mission.task_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "declare_finding"
    assert rows[0]["exit_code"] == 0
    assert rows[0]["raw_ref"] == str(evidence)
    assert rows[0]["step_id"].startswith("worker-workerA-")  # 无主循环步，前缀可辨
