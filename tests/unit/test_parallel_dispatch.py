"""M5-12 并发派单测试：声明面 / 真并发（Barrier 铁证）/ 混合批串行 / 装配口径。

口径：只有"全批 parallel_safe + 开关开启 + 批大小≥2"才并发；回执顺序恒等于
调用顺序；混入一个非 parallel_safe 工具整批串行（不重排副作用）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping

import pytest

from secpt.core.loop import LoopResult, PentestLoop
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import AgentRunTool, DeclareFindingTool, NmapTool
from secpt.tools.guard import GuardConfig
from secpt.tools.registry import ToolManifest, ToolOutput, ToolRegistry


class _Recorder:
    """并发观测器：记录每个工具在哪个线程跑（顺序 + 线程名）。"""

    def __init__(self) -> None:
        self.threads: list[str] = []
        self.lock = threading.Lock()

    def note(self) -> None:
        with self.lock:
            self.threads.append(threading.current_thread().name)


class _Tool:
    """测试用内嵌工具：可选 Barrier 强制并发点（串行执行必然超时炸掉）。"""

    def __init__(
        self,
        name: str,
        recorder: _Recorder,
        *,
        safe: bool,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.manifest = ToolManifest(
            name=name,
            kind="in-process",
            description="并发测试桩",
            params={},
            required=(),
            parallel_safe=safe,
        )
        self._recorder = recorder
        self._barrier = barrier

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        self._recorder.note()
        if self._barrier is not None:
            self._barrier.wait(timeout=2.0)  # 两个并发执行者同时到齐才放行
        return ToolOutput(
            tool_name=self.manifest.name, args=dict(args), exit_code=0, summary="ok", raw_ref=""
        )


def _step_json(*names: str) -> str:
    return json.dumps(
        {
            "thought": "并发派单",
            "plan_updates": [],
            "tool_calls": [{"name": name, "args": {}} for name in names],
            "done": True,
        },
        ensure_ascii=False,
    )


def _run(registry: ToolRegistry, *, parallel: bool = True) -> tuple[sqlite3.Connection, LoopResult]:
    conn = migrate_store.connect(":memory:")
    goal = json.dumps(
        {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"}, ensure_ascii=False
    )
    fake = FakeLLM([goal, _step_json("a", "b")])
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        registry,
        exit_checker=ExitChecklist(),
        parallel_dispatch=parallel,
    )
    return conn, loop.run("并发演示 10.0.0.5")


# ---------- 声明面 --------------------------------------------------------------


class TestDeclarations:
    def test_builtin_tools_declare_parallel_safety(self) -> None:
        assert NmapTool().manifest.parallel_safe is True
        assert AgentRunTool().manifest.parallel_safe is True
        assert DeclareFindingTool().manifest.parallel_safe is False  # 写账本类不并发

    def test_registry_query_defaults_to_false(self) -> None:
        registry = ToolRegistry([_Tool("solo", _Recorder(), safe=True)])
        assert registry.parallel_safe("solo") is True
        assert registry.parallel_safe("ghost") is False


# ---------- 真并发 / 串行 --------------------------------------------------------


class TestDispatchBatch:
    def test_all_safe_batch_runs_concurrently(self) -> None:
        """Barrier(2) 是铁证：串行执行时第一个工具等不到第二个，会超时炸掉。"""
        recorder = _Recorder()
        barrier = threading.Barrier(2)
        registry = ToolRegistry(
            [
                _Tool("a", recorder, safe=True, barrier=barrier),
                _Tool("b", recorder, safe=True, barrier=barrier),
            ]
        )
        conn, result = _run(registry)
        assert result.outcomes[0].ticket_count == 2  # 回执两张、顺序稳定
        assert len(recorder.threads) == 2
        assert all(name.startswith("secpt-dispatch") for name in recorder.threads)
        assert len(set(recorder.threads)) == 2  # 两个不同工作线程
        count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        assert count == 2
        assert ledger.latest_task(conn).exit_kind == "normal"

    def test_mixed_batch_falls_back_to_sequential(self) -> None:
        """混入一个非 parallel_safe 工具 → 整批串行（全部在主线程执行）。"""
        recorder = _Recorder()
        registry = ToolRegistry([_Tool("a", recorder, safe=True), _Tool("b", recorder, safe=False)])
        _conn, _result = _run(registry)
        assert recorder.threads == [threading.main_thread().name] * 2

    def test_switch_off_forces_sequential(self) -> None:
        recorder = _Recorder()
        registry = ToolRegistry([_Tool("a", recorder, safe=True), _Tool("b", recorder, safe=True)])
        _conn, _result = _run(registry, parallel=False)
        assert recorder.threads == [threading.main_thread().name] * 2

    def test_single_call_never_spawns_pool(self) -> None:
        recorder = _Recorder()
        registry = ToolRegistry([_Tool("a", recorder, safe=True)])
        conn = migrate_store.connect(":memory:")
        goal = json.dumps(
            {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"},
            ensure_ascii=False,
        )
        fake = FakeLLM([goal, _step_json("a")])
        loop = PentestLoop(conn, MainLlm(LlmClient(fake)), registry, exit_checker=ExitChecklist())
        loop.run("单调用演示 10.0.0.5")
        assert recorder.threads == [threading.main_thread().name]


# ---------- 装配口径（main._parallel_allowed） -----------------------------------


class _BarrierWorker:
    """worker 替身：同一 Barrier(2) 证明两张单真的同时在干（串行时先到者超时）。"""

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier

    def run(self, mission: object) -> object:
        from secpt.core.mission_board import MissionResult

        self._barrier.wait(timeout=3.0)
        return MissionResult(
            mission_id=getattr(mission, "mission_id", "m"),
            ok=True,
            finished=True,
            steps_taken=1,
            findings_count=0,
            summary="parallel-ok",
        )


def test_two_missions_run_in_parallel_on_board() -> None:
    """端到端：一步两张 agent_run 单 → 任务板两个 worker 同时开干（L12"多线程"）。"""
    from secpt.core.mission_board import MissionBoard
    from secpt.tools.adapters import AgentRunTool

    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "并发派单演示", goal="g")
    barrier = threading.Barrier(2)
    board = MissionBoard(
        conn, lambda _index: _BarrierWorker(barrier), size=2, task_resolver=lambda _c: task.id
    )
    goal = json.dumps(
        {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"}, ensure_ascii=False
    )
    step = json.dumps(
        {
            "thought": "双单并发",
            "plan_updates": [],
            "tool_calls": [
                {
                    "name": "agent_run",
                    "args": {"mission": "单1", "persona": "recon", "step_budget": "1"},
                },
                {
                    "name": "agent_run",
                    "args": {"mission": "单2", "persona": "recon", "step_budget": "1"},
                },
            ],
            "done": True,
        },
        ensure_ascii=False,
    )
    fake = FakeLLM([goal, step])
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        ToolRegistry([AgentRunTool(board=board)]),
        exit_checker=ExitChecklist(),
    )
    try:
        result = loop.run("并发派单演示 10.0.0.5")
    finally:
        board.shutdown()
    assert result.finished_naturally is True
    summaries = [
        row["summary"] for row in conn.execute("SELECT summary FROM tickets ORDER BY rowid")
    ]
    assert len(summaries) == 2 and all("parallel-ok" in text for text in summaries)


class TestAssemblyGate:
    def test_guard_absent_allows_parallel(self) -> None:
        from secpt.main import _parallel_allowed

        assert _parallel_allowed(None, False) is True

    @pytest.mark.parametrize(
        ("hitl_mode", "probe", "expected"),
        [
            ("auto", False, True),
            ("off", False, True),
            ("prompt", False, False),  # stdin 会被多线程抢
            ("auto", True, False),  # 探针 start/stop 是全局态
        ],
    )
    def test_global_state_forces_serial(self, hitl_mode: str, probe: bool, expected: bool) -> None:
        from secpt.main import _parallel_allowed

        guard = GuardConfig(tiers={}, hitl_mode=hitl_mode)
        assert _parallel_allowed(guard, probe) is expected
