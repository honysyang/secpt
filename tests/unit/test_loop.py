"""主循环 contract（FakeLLM 双步全Fake）：FakeLLM 进场→两步→小票落账→事件序列。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from types import SimpleNamespace

import pytest

from secpt.core import checkpointer
from secpt.core.loop import PentestLoop, ToolOutput
from secpt.errors import LedgerError, LlmError, SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.ob.events import EventBus
from secpt.store import ledger, migrate
from secpt.store.experience import record_experience

GOAL_JSON = json.dumps(
    {
        "target": "10.0.0.5",
        "objective": "确认 ssh 与 web 暴露面",
        "success_criteria": "小票中记录两个端口的 banner",
    },
    ensure_ascii=False,
)


def _step_json(thought: str, *, calls: list[dict[str, object]], done: bool) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": calls, "done": done},
        ensure_ascii=False,
    )


class FakeDispatcher:
    """不执行真实工具：只署名一份固定回执，验证 loop 逐个 dispatch + 落小票。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        self.calls.append((name, dict(args)))
        return ToolOutput(
            tool_name=name,
            args=dict(args),
            exit_code=0,
            summary=f"{name} 完成",
            raw_ref=f"runs/fake/{name}.txt",
        )


class SpyDistiller:
    """蒸馏 hook 探针：记 (task_id, env, 所见 exit_kind)——验证"先账后蒸"时点。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def distill(self, conn: sqlite3.Connection, task_id: str, environment: str) -> list[object]:
        row = conn.execute("SELECT exit_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.calls.append((task_id, environment, row["exit_kind"] if row else None))
        return []


class ExplodingDistiller:
    """故障注入蒸馏器：验证 learning 降级不挡收束（W3-1 红线）。"""

    def distill(self, conn: sqlite3.Connection, task_id: str, environment: str) -> list[object]:
        raise RuntimeError("蒸馏器故障注入")


@pytest.fixture()
def rig() -> Callable[..., SimpleNamespace]:
    def _make(
        script: list[object], *, distiller: object | None = None, experience_env: str = "lab"
    ) -> SimpleNamespace:
        conn = migrate.connect(":memory:")
        fake = FakeLLM(script)  # type: ignore[arg-type]
        dispatcher = FakeDispatcher()
        events: list[tuple[str, Mapping[str, object]]] = []
        bus = EventBus()
        bus.subscribe(lambda name, payload: events.append((name, payload)))
        loop = PentestLoop(
            conn,
            MainLlm(LlmClient(fake)),  # type: ignore[arg-type]
            dispatcher,
            bus=bus,
            exit_checker=ExitChecklist(),
            distiller=distiller,  # type: ignore[arg-type]
            experience_env=experience_env,
        )
        return SimpleNamespace(
            conn=conn, fake=fake, loop=loop, dispatcher=dispatcher, events=events
        )

    return _make


def test_two_step_run_writes_ledger_and_emits_events(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    t = rig(
        [
            GOAL_JSON,
            _step_json(
                "先扫 80 端口", calls=[{"name": "nmap", "args": {"ports": "80"}}], done=False
            ),
            _step_json("没有新目标了", calls=[], done=True),
        ]
    )
    result = t.loop.run("测试主机 10.0.0.5")
    assert result.finished_naturally is True
    assert result.steps_taken == 2
    # 工具被逐个点名一次、小票落了一张
    assert t.dispatcher.calls == [("nmap", {"ports": "80"})]
    tickets = t.conn.execute("SELECT * FROM tickets").fetchall()
    assert len(tickets) == 1
    assert tickets[0]["tool_name"] == "nmap"
    assert tickets[0]["exit_code"] == 0
    assert tickets[0]["raw_ref"] == "runs/fake/nmap.txt"
    # steps 两行都挂了 LLM 原文
    steps = t.conn.execute("SELECT * FROM steps ORDER BY step_no").fetchall()
    assert [s["step_no"] for s in steps] == [1, 2]
    assert json.loads(steps[0]["llm_output"])["thought"] == "先扫 80 端口"
    # 事件序列：step1 → 小票 → step2 → 出场三验过 → 收工
    names = [name for name, _ in t.events]
    assert names == [
        "run_step_started",
        "ticket_appended",
        "run_step_started",
        "exit_passed",
        "run_ended",
    ]
    ended = dict(t.events[-1][1])
    assert ended["finished"] is True and ended["steps"] == 2
    # run 收尾：status=ended
    run = t.conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert run["status"] == "ended"


def test_goal_projection_read_from_ledger(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """进场三问存在账本里而非内存：第二步的注意力包也带三问。"""
    t = rig(
        [
            GOAL_JSON,
            _step_json("第一步", calls=[], done=False),
            _step_json("收工", calls=[], done=True),
        ]
    )
    t.loop.run("摸底 10.0.0.5")
    first_call = t.fake.calls[1]  # 第一次 step 调用的消息
    user_msg = first_call[1].content
    assert "目标: 10.0.0.5" in user_msg
    assert "目的: 确认 ssh 与 web 暴露面" in user_msg


def test_empty_goal_rejected_before_llm(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    t = rig([])
    with pytest.raises(ValueError, match="空 goal"):
        t.loop.run("   ")
    assert t.fake.call_count == 0  # 三问必须过账本前先拒，不浪费 LLM 调用


def test_max_steps_exhaustion_not_natural_finish(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """涨停：done 永远不来，跑满步线后 finished_naturally=False，且收尾落账。"""
    t = rig(
        [
            GOAL_JSON,
            # 三条"不收工"的步：涨停由步线先触发，不靠脚本耗尽
            _step_json("继续磨", calls=[], done=False),
            _step_json("继续磨", calls=[], done=False),
            _step_json("继续磨", calls=[], done=False),
        ]
    )
    result = t.loop.run("摸底", max_steps=3)
    assert result.finished_naturally is False
    assert result.steps_taken == 3
    assert len(t.events) and t.events[-1][0] == "run_ended"


def test_llm_error_exits_abnormal_and_propagates(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """异常出口（W1-6）：LlmError 原样上抛给 main.py 兜底，但收尾先落全账——
    该步记 voided、run 收尾、tasks.exit_kind=abnormal，绝不让账悬在半空。"""
    t = rig([GOAL_JSON, LlmError("协议层炸了", code="E_SCHEMA")])
    with pytest.raises(LlmError, match="协议层炸了"):
        t.loop.run("摸底")
    tasks = t.conn.execute("SELECT * FROM tasks").fetchall()
    assert len(tasks) == 1  # 进场已落账，炸在第一步
    assert tasks[0]["exit_kind"] == "abnormal"
    steps = t.conn.execute("SELECT * FROM steps").fetchall()
    assert len(steps) == 1 and steps[0]["status"] == "voided"
    run = t.conn.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "ended"


# ---- W1-5 resume：只复建不回放 -------------------------------------------------


def test_resume_continues_from_waterline(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """手工铺 kill 现场账本 → resume：step_no 从水位线+1 续、run id 复用、不回放。"""
    t = rig([_step_json("踩点 22", calls=[{"name": "probe1"}], done=True)])
    # 铺被 kill 的账本：task + run + 3 步（1 步有小票）+ 水位线=3，status=running
    conn = t.conn
    task = ledger.start_task(conn, "kill 注入主机", goal="10.0.0.5")
    run = ledger.start_run(
        conn,
        task.id,
        config={"objective": "确认暴露面", "success_criteria": "小票记端口 banner"},
    )
    step = ledger.record_step(conn, task.id, run.id, 1, _step_json("断电前", calls=[], done=False))
    ledger.append_ticket(conn, task.id, run.id, step.id, "nmap", {}, 0, "扫过", "raw/a.txt")
    for n in (2, 3):
        ledger.record_step(conn, task.id, run.id, n, _step_json(f"续走 {n}", calls=[], done=False))
    checkpointer.mark_step(conn, run.id, 3)
    assert ledger.get_run(conn, run.id).status == "running"

    result = t.loop.resume(task.id)
    assert result.run_id == run.id  # 复用原 run：run id 跨 kill 连续
    assert result.steps_taken == 1
    assert result.finished_naturally is True
    # 只对续跑的第一步（step4）调了一次 LLM——1~3 没有回放
    assert t.fake.call_count == 1
    steps = conn.execute("SELECT step_no, llm_output FROM steps ORDER BY step_no").fetchall()
    assert [s["step_no"] for s in steps] == [1, 2, 3, 4]
    assert json.loads(steps[3]["llm_output"])["thought"] == "踩点 22"
    run_after = ledger.get_run(conn, run.id)
    assert run_after.step_count == 4 and run_after.status == "ended"
    # 续跑进场包从账本还原：目标投影带 tasks.goal
    user_msg = t.fake.calls[0][1].content
    assert "目标: 10.0.0.5" in user_msg


def test_resume_refuses_when_run_already_ended(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """正常收尾的 run 拒续：LedgerError 上抛，一次 LLM 都不调。"""
    t = rig([_step_json("收工", calls=[], done=True)])
    conn = t.conn
    task = ledger.start_task(conn, "已完成的主机", goal="10.0.0.5")
    run = ledger.start_run(conn, task.id, config={})
    ledger.record_step(conn, task.id, run.id, 1, _step_json("做完", calls=[], done=True))
    checkpointer.mark_step(conn, run.id, 1)
    ledger.end_run(conn, run.id)
    with pytest.raises(LedgerError, match="正常收尾"):
        t.loop.resume(task.id)
    assert t.fake.call_count == 0


# ---- W2-1 出场三验：弹回 WF2 / 连败 3 轮走异常出口 ------------------------------


def _preset_with_claim(t: SimpleNamespace) -> str:
    """铺"三验必不过"的账本现场：task + run + 一个 claimed 悬置 finding。"""
    conn = t.conn
    task = ledger.start_task(conn, "试场主机", goal="10.0.0.5")
    ledger.start_run(
        conn, task.id, config={"objective": "确认暴露面", "success_criteria": "小票记 banner"}
    )
    finding = ledger.append_finding(conn, task.id, "悬置发现", "待裁决", "raw/a.txt")
    return finding.id


def test_checklist_failure_bounces_back_then_passes(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """三验不过弹回 WF2：exit_failed 后继续走步；LLM 裁决 finding verified 再过验。"""
    state: dict[str, object] = {}  # conn/finding 在 rig 建好后才存在，用 dict 中转给脚本

    def _rescue_and_wrap_up() -> str:
        ledger.set_finding_state(state["conn"], str(state["finding"]), "verified")
        return _step_json("裁决完再收工", calls=[], done=True)

    t = rig(
        [
            # resume 不调进场；第 1 步就收工 → 三验挡下（claimed 悬置）
            _step_json("第一轮收工", calls=[], done=True),
            _rescue_and_wrap_up,  # ScriptItem 含 Callable，可直接入队
        ]
    )
    conn = t.conn
    state["conn"] = conn
    state["finding"] = _preset_with_claim(t)
    result = t.loop.resume(conn.execute("SELECT id FROM tasks").fetchone()[0])
    assert result.finished_naturally is True and result.exit_kind == "normal"
    names = [name for name, _ in t.events]
    assert names == [
        "run_step_started",
        "exit_failed",
        "run_step_started",
        "exit_passed",
        "run_ended",
    ]
    assert t.events[1][1]["streak"] == 1  # 弹回而非直接失败
    tasks = conn.execute("SELECT exit_kind FROM tasks").fetchall()
    assert tasks[0]["exit_kind"] == "normal"


def test_checklist_three_round_failure_raises_wall(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """连败 3 轮：E_WALL 原样上抛，但按错误矩阵先落账（stalled + audit + run_ended）。"""
    t = rig(
        [
            _step_json("收工1", calls=[], done=True),
            _step_json("收工2", calls=[], done=True),
            _step_json("收工3", calls=[], done=True),
        ]
    )
    conn = t.conn
    _preset_with_claim(t)
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
    with pytest.raises(SecptError) as exc_info:
        t.loop.resume(task_id)
    assert exc_info.value.code == "E_WALL"
    tasks = conn.execute("SELECT exit_kind FROM tasks").fetchall()
    assert tasks[0]["exit_kind"] == "stalled"  # E_WALL ∈ 失速集
    runs = conn.execute("SELECT status FROM runs").fetchall()
    assert runs[0]["status"] == "ended"
    names = [name for name, _ in t.events]
    assert names.count("exit_failed") == 3 and names[-1] == "run_ended"
    audits = conn.execute("SELECT what FROM audit_log WHERE task_id = ?", (task_id,)).fetchall()
    assert any("E_WALL" in row["what"] for row in audits)


# ---- W3-1 收尾蒸馏 hook：先账后蒸 / 降级不挡收束 / 回放进包 ----------------------


def test_distill_hook_runs_after_ledger_closed(
    rig: Callable[..., SimpleNamespace],
) -> None:
    """正常收束时蒸馏 hook 只在收尾烧一次，且所触发时 exit_kind 已落账（先账后蒸）。"""
    spy = SpyDistiller()
    t = rig(
        [
            GOAL_JSON,
            _step_json("收工", calls=[], done=True),
        ],
        distiller=spy,
    )
    result = t.loop.run("摸底 10.0.0.5")
    assert result.finished_naturally is True
    assert spy.calls == [(result.task_id, "lab", "normal")]


def test_distill_failure_degrades_and_keeps_close(
    rig: Callable[..., SimpleNamespace],
) -> None:
    """learning 可降级：蒸馏器当场爆炸也不改变收束结论，audit 留降级告警。"""
    t = rig([GOAL_JSON, _step_json("收工", calls=[], done=True)], distiller=ExplodingDistiller())
    result = t.loop.run("摸底 10.0.0.5")
    assert result.finished_naturally is True and result.exit_kind == "normal"
    assert t.events[-1][0] == "run_ended"
    run = t.conn.execute("SELECT status FROM runs").fetchone()
    assert run["status"] == "ended"
    audits = t.conn.execute("SELECT what FROM audit_log WHERE actor = 'main'").fetchall()
    assert any("经验蒸馏降级" in row["what"] for row in audits)


def test_experience_replay_enters_attention_pack(
    rig: Callable[..., SimpleNamespace],
) -> None:
    """W3-1 接线：同环境经验经 fetch_replay 投影进包——claimed 命令藏住、异环境不混。"""
    t = rig(
        [_step_json("收工", calls=[], done=True)], distiller=SpyDistiller(), experience_env="prod"
    )
    conn = t.conn
    task = ledger.start_task(conn, "回放场主机", goal="10.0.0.5")
    ledger.start_run(
        conn, task.id, config={"objective": "确认暴露面", "success_criteria": "banner"}
    )
    record_experience(
        conn, task.id, "prod", "claimed", "端口 22 曾验证开放", "nmap -p 22", "runs/r.txt"
    )
    record_experience(conn, task.id, "lab", "verified", "lab 环境的经验", None, "runs/l.txt")
    t.loop.resume(task.id)
    user_msg = t.fake.calls[0][1].content
    assert "## 可用经验" in user_msg
    assert "[claimed|仅当线索] 端口 22 曾验证开放" in user_msg
    assert "nmap -p 22" not in user_msg  # claimed 命令藏住
    assert "lab 环境的经验" not in user_msg  # 异环境不混包
