"""M4-4 审批代理（ApprovalBroker）契约测试。

任务书口径：worker 线程内 HIGH 动作不得直读 stdin——经 ApprovalBroker
（主线程/队列转发，CLI prompt 档由主循环代问）裁决；批/拒两路落 audit
（workerA 发起 + 代理裁决 actor 分层可辨）；未装代理 → fail-closed 保持
现语义；broker 死锁/超时须有界（超时=拒绝，不悬死）。

测试面：
- 纯 broker：批/拒/超时拒绝/无服务员不悬死/空转/清场（queue+future，
  无账本依赖）；
- registry 级：broker 作为 Approver 过 HIGH 闸（hitl_note=人工）；
- worker 级：发起侧 audit 两路各有账（actor=workerA）+ 裁决侧 sink 回调；
- e2e：AgentRunTool 收拢等待窗内代问（主循环 serve，worker 真过闸）。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from secpt.core.mission_board import Mission, MissionBoard
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.worker import Worker
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import AgentRunTool, NmapTool
from secpt.tools.broker import ApprovalBroker
from secpt.tools.guard import GuardConfig
from secpt.tools.registry import ToolRegistry


class _FakeDelegate:
    """真审批人替身：记录被问过的工具名，可设延迟与裁决。"""

    def __init__(self, approved: bool = True, *, delay_s: float = 0.0) -> None:
        self.kind = "human"
        self.approved = approved
        self.delay_s = delay_s
        self.asked: list[str] = []

    def decide(self, name: str) -> bool:
        self.asked.append(name)
        if self.delay_s:
            time.sleep(self.delay_s)
        return self.approved


def _serve_loop(broker: ApprovalBroker, stop: threading.Event) -> threading.Thread:
    """后台服务员：模拟主线程在等待窗内持续 serve（单元测试的"主循环"替身）。"""

    def body() -> None:
        while not stop.is_set():
            broker.serve_once(timeout_s=0.05)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


def _step(thought: str, *, calls: list[dict[str, object]] | None = None, done: bool = False) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": calls or [], "done": done},
        ensure_ascii=False,
    )


# ---------- 纯 broker（有界性与转发链） ----------------------------------------------


class TestBrokerBoundedness:
    def test_approve_path_bounded_and_sink_notified(self) -> None:
        """批路：入队 → 主线程 serve → worker 拿到 True；sink 如实收 (name, True, kind)。"""
        decisions: list[tuple[str, bool, str]] = []
        broker = ApprovalBroker(
            _FakeDelegate(True),
            timeout_s=5.0,
            on_decision=lambda n, ok, via: decisions.append((n, ok, via)),
        )
        answers: list[bool] = []
        worker = threading.Thread(target=lambda: answers.append(broker.decide("nmap")))
        worker.start()
        verdict = broker.serve_once(timeout_s=2.0)
        worker.join(timeout=5)
        assert verdict is True
        assert answers == [True]
        assert decisions == [("nmap", True, "human")]

    def test_deny_path_sink_notified(self) -> None:
        """拒路：裁决 False 原样回传，两路对称（不存在"只有批才有账"）。"""
        decisions: list[tuple[str, bool, str]] = []
        broker = ApprovalBroker(
            _FakeDelegate(False),
            timeout_s=5.0,
            on_decision=lambda n, ok, via: decisions.append((n, ok, via)),
        )
        answers: list[bool] = []
        worker = threading.Thread(target=lambda: answers.append(broker.decide("nmap")))
        worker.start()
        verdict = broker.serve_once(timeout_s=2.0)
        worker.join(timeout=5)
        assert verdict is False
        assert answers == [False]
        assert decisions == [("nmap", False, "human")]

    def test_timeout_denies_and_late_answer_does_not_revive(self) -> None:
        """超时=拒绝：worker 有界拿到 False；主线程晚到的答复不改判已发回执。"""
        delegate = _FakeDelegate(True, delay_s=0.8)  # 人想太久
        broker = ApprovalBroker(delegate, timeout_s=0.2)
        answers: list[bool] = []
        worker = threading.Thread(target=lambda: answers.append(broker.decide("nmap")))
        worker.start()
        start = time.monotonic()
        worker.join(timeout=5)
        elapsed = time.monotonic() - start
        assert answers == [False]  # worker 侧拒绝落袋
        assert elapsed < 2.0  # 有界：不等人的全部思考时间
        served = broker.serve_once(timeout_s=2.0)  # 晚到的裁决
        assert served is True  # 裁决行照常落账（sink 口径如实）
        assert delegate.asked == ["nmap"]

    def test_no_server_bounded_not_hung(self) -> None:
        """无服务员（主线程死了/没进等待窗）：decide 超时拒绝，绝不永久挂起。"""
        broker = ApprovalBroker(_FakeDelegate(True), timeout_s=0.3)
        start = time.monotonic()
        assert broker.decide("nmap") is False
        assert time.monotonic() - start < 2.0

    def test_serve_once_empty_returns_none(self) -> None:
        broker = ApprovalBroker(_FakeDelegate(True), timeout_s=5.0)
        assert broker.serve_once() is None
        assert broker.pending() == 0

    def test_deny_pending_drains_all(self) -> None:
        """清场：收板时未裁决请求全部当面拒绝，不悬死。"""
        broker = ApprovalBroker(_FakeDelegate(True), timeout_s=5.0)
        answers: list[bool] = []
        workers = [
            threading.Thread(target=lambda i=i: answers.append(broker.decide(f"tool{i}")))
            for i in range(3)
        ]
        for thread in workers:
            thread.start()
        time.sleep(0.1)  # 等入队
        assert broker.deny_pending() == 3
        for thread in workers:
            thread.join(timeout=5)
        assert answers == [False, False, False]

    def test_delegate_crash_denies(self) -> None:
        """真审批人炸了 = 拒绝（fail-closed），不向 worker 抛异常。"""
        broker = ApprovalBroker(_FakeDelegate(True), timeout_s=5.0)

        def boom(name: str) -> bool:
            raise RuntimeError("审批人失联")

        broker._delegate = type("Boom", (), {"kind": "human", "decide": staticmethod(boom)})()
        answers: list[bool] = []
        worker = threading.Thread(target=lambda: answers.append(broker.decide("nmap")))
        worker.start()
        verdict = broker.serve_once(timeout_s=2.0)
        worker.join(timeout=5)
        assert verdict is False and answers == [False]


# ---------- registry 级：broker 就是 Approver（同权过闸） -----------------------------


def test_broker_passes_high_gate_with_human_note(tmp_path: Path) -> None:
    """broker 过 Guard 闸的回执语义与真人审批一致：hitl_approved + note=人工。"""
    delegate = _FakeDelegate(True)
    broker = ApprovalBroker(delegate, timeout_s=5.0)
    registry = ToolRegistry(
        [NmapTool(binary="/bin/echo", raw_dir=tmp_path)],
        guard=GuardConfig(tiers={"nmap": "high"}, hitl_mode="prompt"),
        approver=broker,
    )
    stop = threading.Event()
    server = _serve_loop(broker, stop)  # "主循环"替身在等待窗内代问
    try:
        receipt = registry.dispatch("nmap", {"target": "10.0.0.5"})
    finally:
        stop.set()
        server.join(timeout=5)

    assert receipt.exit_code == 0
    assert receipt.hitl_approved is True
    assert receipt.hitl_note == "人工"
    assert delegate.asked == ["nmap"]


# ---------- worker 级：发起侧 audit 两路各有账 ----------------------------------------


@pytest.mark.parametrize("approved", [True, False])
def test_worker_high_via_broker_initiator_audit_both_paths(tmp_path: Path, approved: bool) -> None:
    """批/拒两路：workerA 发起侧各有 audit 账（mock 两路各有账，任务书口径）。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, input_text="m44 审批代理", goal="摸扫")
    decisions: list[tuple[str, bool, str]] = []
    broker = ApprovalBroker(
        _FakeDelegate(approved),
        timeout_s=5.0,
        on_decision=lambda n, ok, via: decisions.append((n, ok, via)),
    )
    stop = threading.Event()
    server = _serve_loop(broker, stop)
    script = [
        _step("起扫", calls=[{"name": "nmap", "args": {"target": "10.0.0.5"}}]),
        _step("收工", done=True),
    ]
    registry = ToolRegistry(
        [NmapTool(binary="/bin/echo", raw_dir=tmp_path)],
        guard=GuardConfig(tiers={"nmap": "high"}, hitl_mode="prompt"),
        approver=broker,
    )
    worker = Worker(llm=LlmClient(FakeLLM(script)), dispatcher=registry, conn=conn)
    mission = Mission(persona="worker", goal="领单摸扫", task_id=task.id, step_budget=4)
    try:
        result = worker.run(mission)
    finally:
        stop.set()
        server.join(timeout=5)

    assert (result.ok, result.finished) == (True, True)
    assert decisions == [("nmap", approved, "human")]
    worker_rows = [
        row[0]
        for row in conn.execute(
            "SELECT what FROM audit_log WHERE task_id=? AND actor='workerA'", (task.id,)
        ).fetchall()
    ]
    if approved:
        assert any("HIGH 审批发起" in w and "批准" in w for w in worker_rows)
    else:
        assert any("HIGH 审批发起" in w and "拒绝" in w for w in worker_rows)


def test_worker_without_broker_stays_fail_closed(tmp_path: Path) -> None:
    """未装代理（approver=None）：HIGH 拒绝回执 + 发起侧仍有拒绝账（不静默）。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, input_text="m44 无代理", goal="摸扫")
    script = [
        _step("起扫", calls=[{"name": "nmap", "args": {"target": "10.0.0.5"}}]),
        _step("收工", done=True),
    ]
    registry = ToolRegistry(
        [NmapTool(binary="/bin/echo", raw_dir=tmp_path)],
        guard=GuardConfig(tiers={"nmap": "high"}, hitl_mode="prompt"),
        approver=None,  # 未装代理
    )
    worker = Worker(llm=LlmClient(FakeLLM(script)), dispatcher=registry, conn=conn)
    mission = Mission(persona="worker", goal="领单摸扫", task_id=task.id, step_budget=4)
    result = worker.run(mission)

    assert (result.ok, result.finished) == (True, True)  # 拒绝不炸 worker
    worker_rows = [
        row[0]
        for row in conn.execute(
            "SELECT what FROM audit_log WHERE task_id=? AND actor='workerA'", (task.id,)
        ).fetchall()
    ]
    assert any("HIGH 审批发起" in w and "拒绝" in w for w in worker_rows)


# ---------- e2e：AgentRunTool 收拢等待窗内代问 ----------------------------------------


def _worker_builder_for_board(
    script: list[str], tmp_path: Path, broker: ApprovalBroker
) -> Callable[[int], Worker]:
    """board 级 builder：worker registry 挂 broker（prompt 档语义），nmap=HIGH。"""

    def build(index: int) -> Worker:
        # worker 独立 ：memory: 账本（e2e 只验链路，发起侧审计另有专项用例）。
        worker_conn = migrate_store.connect(":memory:")
        registry = ToolRegistry(
            [NmapTool(binary="/bin/echo", raw_dir=tmp_path)],
            guard=GuardConfig(tiers={"nmap": "high"}, hitl_mode="prompt"),
            approver=broker,
        )
        return Worker(
            llm=LlmClient(FakeLLM(list(script))),
            dispatcher=registry,
            conn=worker_conn,
            audit_actor=f"worker{chr(ord('A') + index)}",
        )

    return build


def test_agent_run_tool_serves_broker_while_collecting(tmp_path: Path) -> None:
    """主循环收拢 worker 小票期间代问审批——单线程通道，不需要专职服务员。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, input_text="e2e 代理审批", goal="摸扫")
    broker = ApprovalBroker(_FakeDelegate(True), timeout_s=5.0)
    script = [
        _step("起扫", calls=[{"name": "nmap", "args": {"target": "10.0.0.5"}}]),
        _step("收工", done=True),
    ]
    board = MissionBoard(
        conn,
        _worker_builder_for_board(script, tmp_path, broker),
        size=1,
        task_resolver=lambda _c: task.id,
    )
    try:
        main_registry = ToolRegistry([AgentRunTool(board=board, broker=broker)])
        receipt = main_registry.dispatch(
            "agent_run", {"mission": "扫一把", "persona": "worker", "step_budget": "4"}
        )
    finally:
        board.shutdown()

    assert receipt.exit_code == 0
    assert "收拢" in receipt.summary
    assert broker.pending() == 0  # 请求被 serve 干净，不积压


@pytest.mark.parametrize("approved", [True, False])
def test_broker_kind_delegates_honestly(approved: bool) -> None:
    """kind 透传真审批人：代理不冒充人类也不冒充自动（账面诚实）。"""
    assert ApprovalBroker(_FakeDelegate(approved)).kind == "human"


def test_timeout_s_must_be_positive() -> None:
    """timeout_s<=0 = 装配笔误：ValueError 拒装（有界性是构造期契约）。"""
    with pytest.raises(ValueError):
        ApprovalBroker(_FakeDelegate(True), timeout_s=0)
