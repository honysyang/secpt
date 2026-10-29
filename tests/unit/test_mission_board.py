"""M3-5 多 worker 领单（先测试）：MissionBoard 并行证明 + agent_run 真接线全链。

拍板①（§71 前口径）：多 worker 形态 = agent_run 真接线 + 线程池——主 LLM 经
tool_calls 点名 agent_run → MissionBoard 线程池领单 → agent_run 阻塞收取回执
小票（蓝本 L551 契约兑现；主循环逐 call 语义不变）。拍板③（§71 前口径）：
worker 落账口径 = 独立 conn 直写黑板（findings 写口只经 store/*，audit
actor=workerA）；收拢摘要小票由主循环 agent_run 一行落账。

附 §67 遗留②收口的失败注入测试：loop 失速计对 denied 回执（拒绝≠连败，
工具压根没执行）必须跳过——修复前本组断言必红。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import pytest

from secpt.core.loop import PentestLoop
from secpt.core.mission_board import Mission, MissionBoard, MissionResult
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.judge import sweep
from secpt.judge.profile import GroundTruth, JudgeProfile
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.llms.worker import Worker
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import AgentRunTool, DeclareFindingTool
from secpt.tools.guard import Approver, AutoApprover, GuardConfig
from secpt.tools.registry import ToolManifest, ToolOutput, ToolRegistry


class WorkerLike(Protocol):
    """worker 最小契约（工单样板：run(mission) → MissionResult）。"""

    def run(self, mission: Mission) -> MissionResult: ...


# ---------- 测试用 worker 替身（记账/计数型） ----------------------------------------


class _StubWorker:
    """记账替身：每单记一行 owner 标签；指定 goal 可引爆（异常承载测试用）。"""

    def __init__(
        self,
        index: int,
        ledger: dict[str, list[str]],
        *,
        explode_goal: str | None = None,
    ) -> None:
        self._index = index
        self._ledger = ledger
        self._explode_goal = explode_goal

    def run(self, mission: Mission) -> MissionResult:
        if mission.goal == self._explode_goal:
            raise RuntimeError(f"worker-{self._index} 引爆：{mission.goal}")
        self._ledger.setdefault(mission.mission_id, []).append(f"worker-{self._index}")
        return MissionResult(
            mission_id=mission.mission_id,
            ok=True,
            finished=True,
            steps_taken=1,
            findings_count=0,
            summary=f"worker-{self._index} 收单：{mission.goal}",
        )


class _BarrierWorker:
    """并行度证明替身：三 worker 同刻报到同一 Barrier，串行即为死锁超时。"""

    def __init__(self, index: int, barrier: threading.Barrier) -> None:
        self._index = index
        self._barrier = barrier

    def run(self, mission: Mission) -> MissionResult:
        self._barrier.wait(timeout=15.0)
        return MissionResult(
            mission_id=mission.mission_id,
            ok=True,
            finished=True,
            steps_taken=0,
            findings_count=0,
            summary=f"worker-{self._index} 同刻到齐",
        )


def _make_board(
    worker_builder: object,
    *,
    size: int = 3,
    conn: object = None,
    task_resolver: object = None,
) -> MissionBoard:
    """测试装配便捷：未给的凭空造（board 的 resolver conn 用内存库占位）。"""
    return MissionBoard(
        conn if conn is not None else migrate_store.connect(":memory:"),
        worker_builder,
        size=size,
        task_resolver=task_resolver,
    )


# ---------- 领单任务板的反断言 -----------------------------------------------------


def test_mission_result_shape_is_pinned() -> None:
    """MissionResult 形状锁死：票面七字段（收拢摘要小票的契约面）。"""
    res = MissionResult(
        mission_id="m-1", ok=True, finished=True, steps_taken=2, findings_count=1, summary="收单"
    )
    assert res.mission_id == "m-1"
    assert (res.ok, res.finished, res.steps_taken, res.findings_count) == (True, True, 2, 1)


def test_board_delivers_result_to_owner() -> None:
    """submit → 某个 worker 领单 → future 拿到该单收拢小票（一对一）。

    锚点显式（task_id）：本组只验认领/回执语义，不掺 resolver 解析。
    """
    ledgermar: dict[str, list[str]] = {}
    board = _make_board(lambda i: _StubWorker(i, ledgermar), size=2)
    try:
        mission = Mission(persona="recon", goal="摸扫一圈", step_budget=2, task_id="t-1")
        future = board.submit(mission)
        result = future.result(timeout=10)
        assert result.ok is True
        assert result.summary.startswith("worker-")
        assert "摸扫一圈" in result.summary
        assert sum(len(v) for v in ledgermar.values()) == 1
    finally:
        board.shutdown()


def test_board_three_workers_run_concurrently() -> None:
    """线程池并行证明：三 worker 同刻到齐 Barrier——串行板会死锁超时。"""
    barrier = threading.Barrier(3)
    board = _make_board(lambda i: _BarrierWorker(i, barrier), size=3)
    try:
        futures = [
            board.submit(Mission(persona="p", goal=f"单{i}", step_budget=1, task_id=f"t-{i}"))
            for i in range(3)
        ]
        results = [f.result(timeout=15).summary for f in futures]
        assert len({r for r in results if r.startswith("worker-")}) == 3
    finally:
        board.shutdown()


def test_board_each_mission_claimed_exactly_once() -> None:
    """queue 天然原子认领：8 单 = 8 次处理（无重领、无丢单）。"""
    ledgermar: dict[str, list[str]] = {}
    board = _make_board(lambda i: _StubWorker(i, ledgermar), size=3)
    try:
        missions = [
            Mission(persona="p", goal=f"单-{k}", step_budget=1, task_id=f"t-{k}") for k in range(8)
        ]
        futures = [board.submit(m) for m in missions]
        for f in futures:
            assert f.result(timeout=10).ok is True
        assert sum(len(v) for v in ledgermar.values()) == 8
        assert all(len(v) == 1 for v in ledgermar.values())
    finally:
        board.shutdown()


def test_board_worker_exception_carried_and_board_survives() -> None:
    """worker 爆炸：异常原样承载进 future，board 本体不死可继续接单。"""
    ledgermar: dict[str, list[str]] = {}
    boom = "引爆单"
    board = _make_board(lambda i: _StubWorker(i, ledgermar, explode_goal=boom), size=2)
    try:
        future = board.submit(Mission(persona="p", goal=boom, step_budget=1, task_id="t-1"))
        with pytest.raises(RuntimeError, match="引爆"):
            future.result(timeout=10)
        second = board.submit(Mission(persona="p", goal="好单", step_budget=1, task_id="t-2"))
        assert second.result(timeout=10).ok is True
    finally:
        board.shutdown()


# ---------- task_id 解析（领单锚点不假写） ------------------------------------------


def test_board_submit_without_resolver_raises_sync() -> None:
    """无 resolver 且 mission 无 task_id：同步 ValueError，不造 future 不假写。"""
    board = _make_board(lambda i: _StubWorker(i, {}), size=1)
    try:
        with pytest.raises(ValueError, match="task"):
            board.submit(Mission(persona="p", goal="无锚单", step_budget=1))
    finally:
        board.shutdown()


def test_board_empty_ledger_resolver_raises(tmp_path: Path) -> None:
    """resolver 查不到 task（空库 LedgerError）→ 包成 ValueError 上抛，不静默。"""
    conn = migrate_store.connect(str(tmp_path / "empty.sqlite"))
    board = _make_board(
        lambda i: _StubWorker(i, {}),
        size=1,
        conn=conn,
        task_resolver=lambda c: ledger.latest_task(c).id,
    )
    try:
        with pytest.raises(ValueError):
            board.submit(Mission(persona="p", goal="无人认单", step_budget=1))
    finally:
        board.shutdown()
        conn.close()


def test_board_explicit_task_id_skips_resolver(tmp_path: Path) -> None:
    """mission 自带 task_id = 免 resolver 直领（显式锚点高于自动解析）。"""
    conn = migrate_store.connect(str(tmp_path / "empty2.sqlite"))
    board = _make_board(
        lambda i: _StubWorker(i, {}),
        size=1,
        conn=conn,
        task_resolver=lambda c: ledger.latest_task(c).id,
    )
    try:
        future = board.submit(Mission(persona="p", goal="带锚单", step_budget=1, task_id="t-1"))
        assert future.result(timeout=10).ok is True
    finally:
        board.shutdown()
        conn.close()


# ---------- shutdown（排空 + 拒单 + 线程收点） ---------------------------------------


def test_board_shutdown_drains_and_rejects() -> None:
    """shutdown：排空已入队余单 → 拒新单（RuntimeError）→ 线程全部收点。"""
    ledgermar: dict[str, list[str]] = {}
    board = _make_board(lambda i: _StubWorker(i, ledgermar), size=2)
    futures = [
        board.submit(Mission(persona="p", goal=f"尾单-{k}", step_budget=1, task_id=f"t-{k}"))
        for k in range(4)
    ]
    board.shutdown()
    for f in futures:
        assert f.result(timeout=10).ok is True
    with pytest.raises(RuntimeError):
        board.submit(Mission(persona="p", goal="迟到的单", step_budget=1, task_id="t-late"))
    assert all(not t.is_alive() for t in board.threads)


# ---------- AgentRunTool 接线（M1 占位守恒 + board 版） ------------------------------


def test_agent_run_without_board_keeps_m1_placeholder_reply() -> None:
    """board=None：M1 占位回执逐字节守恒（204 例兼容机位，改即破约）。"""
    tool = AgentRunTool()
    receipt = tool.run({"mission": "试试", "persona": "recon", "step_budget": "3"})
    assert receipt.summary == "E_TOOL_FAIL: agent_run M1 占位——worker 执行器 M2 接线"
    assert receipt.exit_code is None
    assert receipt.raw_ref == ""


class _IdleWorker:
    """预算吃满也没收工：ok=True/finished=False（"没炸"≠"有进展"）。"""

    def run(self, mission: Mission) -> MissionResult:
        return MissionResult(
            mission_id=mission.mission_id,
            ok=True,
            finished=False,
            steps_taken=3,
            findings_count=0,
            summary="预算用尽：3 步内未收工，申报 0 条",
        )


def test_agent_run_unfinished_marks_no_progress() -> None:
    """M5-27：未完工回执显式声明 made_progress=False，摘要写明未完工。

    为什么不改 exit_code：ok/finished 两轴是 M3-5 的诚实语义（未完成不是失败）；
    失速计要的是"有没有进展"，由回执独立声明，两个语义各归各的字段。
    """
    board = _make_board(lambda _i: _IdleWorker(), size=1, task_resolver=lambda _c: "t-any")
    tool = AgentRunTool(board=board)
    try:
        receipt = tool.run({"mission": "空转", "persona": "recon", "step_budget": "3"})
    finally:
        board.shutdown()
    assert receipt.exit_code == 0
    assert receipt.made_progress is False
    assert receipt.summary.startswith("agent_run 收拢：")
    assert "（未完工）" in receipt.summary


class _RealWorkerAssembly:
    """e2e worker 装配：每线程独立 FakeLLM（非线程安全，不破 fake.py 契约）。"""

    def __init__(self, db_path: Path, evidence_ref: Path, base_workers: int = 3) -> None:
        self.base_workers = base_workers
        self.db_path = db_path
        self.evidence_ref = evidence_ref
        self.worker_llms: list[FakeLLM] = []

    def build(self, index: int) -> Worker:
        """per-thread Worker：独立 conn + 独立 registry（不含 agent_run——
        从构造上杜绝 worker 嵌套派单死锁）+ 独立 FakeLLM 实例。"""
        llm = LlmClient(FakeLLM([self._worker_step_script()]))
        self.worker_llms.append(llm._provider)  # noqa: SLF001  测试断言用
        registry = ToolRegistry([DeclareFindingTool()])
        return Worker(
            llm=llm,
            dispatcher=registry,
            conn=migrate_store.connect(self.db_path),
        )

    def _worker_step_script(self) -> str:
        """单步脚本：申报一条 finding（证据文件已在档）后收工 done=True。"""
        step = json.dumps(
            {
                "thought": "领单干活：申报发现",
                "plan_updates": [],
                "tool_calls": [
                    {
                        "name": "declare_finding",
                        "args": {
                            "title": "worker 发现：测试边界打开",
                            "detail": "领单 worker 申报的发现",
                            "evidence_ref": str(self.evidence_ref),
                        },
                    }
                ],
                "done": True,
            },
            ensure_ascii=False,
        )
        return step


class _DangerTool:
    """记账 danger 适配器：执行即 calls+=1（§67 遗留②的核心证据替身）。"""

    manifest = ToolManifest(name="danger", kind="mcp", description="测试工具", params={})

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        self.calls += 1
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary="执行过一次",
            raw_ref="",
        )


def test_loop_denied_receipts_skip_stall_counter() -> None:
    """§67 遗留②：denied（拒绝≠连败）不进失速计——6 连拒照走 done 正常收束。

    修复前口径：denied 按 exit_code=None 计失败，第 5 步拉铃 → 无 coach →
    E_STALL 异常出口；修复后 6 张拒绝小票照落账、失速计零数、收工。
    """
    fake = FakeLLM(
        [
            json.dumps(
                {"target": "10.0.0.5", "objective": "o", "success_criteria": "c"},
                ensure_ascii=False,
            )
        ]
        + [
            json.dumps(
                {
                    "thought": f"再试第{k}",
                    "tool_calls": [
                        {"name": "danger", "args": {}},
                    ],
                    "done": False,
                },
                ensure_ascii=False,
            )
            for k in range(6)
        ]
        + [
            json.dumps({"thought": "收工", "tool_calls": [], "done": True}, ensure_ascii=False),
            json.dumps({"summary": "deny 连发后收束"}, ensure_ascii=False),
        ]
    )
    conn = migrate_store.connect(":memory:")
    tool = _DangerTool()
    registry = ToolRegistry([tool], guard=GuardConfig(tiers={"danger": "high"}, hitl_mode="off"))
    loop = PentestLoop(conn, MainLlm(LlmClient(fake)), registry, exit_checker=ExitChecklist())
    result = loop.run("deny 连发 10.0.0.5")
    assert result.finished_naturally is True
    assert tool.calls == 0
    denied = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE tool_name='danger' AND exit_code IS NULL"
    ).fetchone()[0]
    assert denied == 6


def test_e2e_agent_run_board_pipeline(tmp_path: Path) -> None:
    """全链 e2e：主循环点名 agent_run → 板级 3 worker 领单 → worker 有案 → 判票收口。

    四证齐落：①loop finished_naturally（judge R4 oracle 收口 claimed）；②
    finding 落账 verify_state=verified（worker 直写黑板走 ledger 官门）；③
    agent_run 小票一行收拢摘要、exit_code=0；④audit 有 actor=workerA 领单痕。
    """
    db = tmp_path / "pool.sqlite"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    evidence = raw_dir / "worker_evidence.txt"
    evidence.write_text("worker 交付的客观证据\n", encoding="utf-8")

    assembly = _RealWorkerAssembly(db, evidence)
    main_conn = migrate_store.connect(db)
    board = MissionBoard(
        main_conn,
        assembly.build,
        size=3,
        task_resolver=lambda c: ledger.latest_task(c).id,
    )
    agent_tool = AgentRunTool(board=board)
    approver: Approver = AutoApprover()

    fake_main = FakeLLM(
        [
            json.dumps(
                {
                    "target": "10.0.0.9",
                    "objective": "worker 领单演示",
                    "success_criteria": "finding 落账且判票收口",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "thought": "派一单给 worker",
                    "tool_calls": [
                        {
                            "name": "agent_run",
                            "args": {
                                "mission": "摸扫并申报",
                                "persona": "recon",
                                "step_budget": "3",
                            },
                        }
                    ],
                    "done": False,
                },
                ensure_ascii=False,
            ),
            json.dumps({"thought": "收工", "tool_calls": [], "done": True}, ensure_ascii=False),
            json.dumps({"summary": "agent_run 收拢演示"}, ensure_ascii=False),
        ]
    )
    prof = JudgeProfile(
        enabled=True,
        ground_truth=(GroundTruth(id="gt-fetch", pattern="worker 发现"),),
        source="test",
    )
    registry = ToolRegistry(
        [agent_tool],
        guard=GuardConfig(),
        approver=approver,
    )
    try:
        loop = PentestLoop(
            main_conn,
            MainLlm(LlmClient(fake_main)),
            registry,
            exit_checker=ExitChecklist(),
            judge_sweep=lambda c, t: sweep(c, t, prof),
            judge_note="裁判开启（e2e R4 oracle）",
        )
        result = loop.run("agent_run e2e 10.0.0.9")
    finally:
        board.shutdown()

    # 四证逐一核验（board shutdown 后主库读数无并发干扰）
    assert result.finished_naturally is True
    findings = ledger.task_findings(main_conn, result.task_id)
    assert [f.verify_state for f in findings] == ["verified"]
    tickets = main_conn.execute(
        "SELECT summary, exit_code FROM tickets WHERE tool_name='agent_run'"
    ).fetchall()
    assert len(tickets) == 1
    assert tickets[0][1] == 0
    assert tickets[0][0].startswith("agent_run 收拢：")
    worker_audits = main_conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE actor='workerA'"
    ).fetchone()[0]
    assert worker_audits >= 1
    worker_llms = assembly.worker_llms
    assert any(llm.call_count >= 1 for llm in worker_llms)
