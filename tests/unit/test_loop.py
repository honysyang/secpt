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
        "graph_changed",
        "run_step_started",
        "graph_changed",
        "exit_passed",
        "run_ended",
    ]
    ended = dict(t.events[-1][1])
    assert ended["finished"] is True and ended["steps"] == 2
    # M5-29：收尾一行带耗时与发现数；小票事件带单票耗时
    assert isinstance(ended["duration_ms"], int) and ended["findings"] == 0
    ticket_event = next(payload for name, payload in t.events if name == "ticket_appended")
    assert isinstance(ticket_event["duration_ms"], int)
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
        "graph_changed",
        "exit_failed",
        "run_step_started",
        "graph_changed",
        "exit_passed",
        "run_ended",
    ]
    assert t.events[2][1]["streak"] == 1  # 弹回而非直接失败（[2]=exit_failed，[1]=graph_changed）
    tasks = conn.execute("SELECT exit_kind FROM tasks").fetchall()
    assert tasks[0]["exit_kind"] == "normal"
    # M5-28：失败明细随下一步注意力包下发（不只在事件/audit 里）
    second_prompt = t.fake.calls[1][1].content
    assert "【出场未过·逐条修】" in second_prompt
    assert "claimed" in second_prompt


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
    """W3-1 接线 + M3-4 收紧（拍板④）：verified 经验经 fetch_replay 投影进包并
    原样带命令；claimed 判票前不进回放位；异环境不混。"""
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
    record_experience(conn, task.id, "prod", "verified", "端口 22 常开", "nmap -p 22", "runs/v.txt")
    record_experience(conn, task.id, "lab", "verified", "lab 环境的经验", None, "runs/l.txt")
    t.loop.resume(task.id)
    user_msg = t.fake.calls[0][1].content
    assert "## 可用经验" in user_msg
    assert "[verified] 端口 22 常开" in user_msg
    assert "（命令：nmap -p 22）" in user_msg
    assert "端口 22 曾验证开放" not in user_msg  # claimed 判票前不进包（收紧）
    assert "lab 环境的经验" not in user_msg  # 异环境不混包


# ---------- M4-6 双图事件线：主循环写图 + graph_changed 只报告不接管 ----------------


def test_step_writes_graph_node_and_sequence_edge(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """蓝本 L340"同一事务写双图"的落地：每步落 step 节点 + 步进边（探索图活起来）。"""
    t = rig(
        [
            GOAL_JSON,
            _step_json(
                "先扫 80 端口", calls=[{"name": "nmap", "args": {"ports": "80"}}], done=False
            ),
            _step_json("收工", calls=[], done=True),
        ]
    )
    result = t.loop.run("测试主机 10.0.0.5")
    nodes = t.conn.execute(
        "SELECT kind, key FROM graph_nodes WHERE task_id = ? ORDER BY key", (result.task_id,)
    ).fetchall()
    assert [(r["kind"], r["key"]) for r in nodes] == [("step", "step-1"), ("step", "step-2")]
    edges = t.conn.execute(
        "SELECT kind, src, dst FROM graph_edges WHERE task_id = ?", (result.task_id,)
    ).fetchall()
    assert [(r["kind"], r["src"], r["dst"]) for r in edges] == [("sequence", "step-1", "step-2")]


def test_graph_changed_emitted_after_commit_reports_only(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """graph_changed 在事务提交后发（同 ticket_appended 手式），且只报告不接管（R3）。

    只报告的证据：EventBus.emit 无返回值、无订阅者能改执行流——本事件后循环
    照常推进（exit_passed/run_ended 照发），事件序里 graph_changed 必在
    ticket_appended 之后、exit_* 之前（写图→提交→报告→出口判定）。
    """
    t = rig(
        [
            GOAL_JSON,
            _step_json("扫 80", calls=[{"name": "nmap", "args": {"ports": "80"}}], done=False),
            _step_json("收工", calls=[], done=True),
        ]
    )
    result = t.loop.run("测试主机 10.0.0.5")
    names = [name for name, _ in t.events]
    assert names == [
        "run_step_started",
        "ticket_appended",
        "graph_changed",
        "run_step_started",
        "graph_changed",
        "exit_passed",
        "run_ended",
    ]
    payload = dict(t.events[2][1])
    assert payload["task_id"] == result.task_id
    assert payload["step_no"] == 1
    assert payload["nodes"] == 1 and payload["edges"] == 0  # 首步只有节点无前驱边
    payload2 = dict(t.events[4][1])
    assert payload2["step_no"] == 2 and payload2["edges"] == 1


def test_step_audit_row_carries_graph_writes(
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """audit step 行带 graph 写痕：面板 SSE 轮询 audit 增量即见图变化（零新端点）。"""
    t = rig(
        [
            GOAL_JSON,
            _step_json("扫 80", calls=[{"name": "nmap", "args": {"ports": "80"}}], done=False),
            _step_json("收工", calls=[], done=True),
        ]
    )
    t.loop.run("测试主机 10.0.0.5")
    rows = t.conn.execute(
        "SELECT what FROM audit_log WHERE what LIKE 'step %' ORDER BY id"
    ).fetchall()
    whats = [str(r["what"]) for r in rows]
    assert "graph=+1n+0e" in whats[0]  # 首步：step-1 节点
    assert "graph=+1n+1e" in whats[1]  # 次步：step-2 节点 + 步进边


# ---------- M5-27 感知面闭环：工具签名进包 + 未完工计无进展 ---------------------


class CatalogDispatcher(FakeDispatcher):
    """带签名的假 dispatcher：验证 loop 把 registry 目录塞进注意力包。"""

    def tool_catalog(self) -> list[str]:
        return ["nmap(target*:str, ports:str) — 端口扫描"]


class NoProgressDispatcher:
    """exit=0 但不推进：模拟 agent_run 预算用尽（M5-27 的无进展回执）。"""

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        return ToolOutput(
            tool_name=name,
            args=dict(args),
            exit_code=0,
            summary="agent_run 收拢：预算用尽（未完工）",
            raw_ref="",
            made_progress=False,
        )


def test_tool_catalog_enters_attention_pack() -> None:
    """签名每步进包：模型不必再靠"未知参数"回执反推参数名。"""
    conn = migrate.connect(":memory:")
    fake = FakeLLM([GOAL_JSON, _step_json("收工", calls=[], done=True)])
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        CatalogDispatcher(),
        exit_checker=ExitChecklist(),
    )
    loop.run("测试主机 10.0.0.5")
    user_msg = fake.calls[1][1].content  # calls[0] 是进场三问，calls[1] 才是首步注意力包
    assert "## 可用工具" in user_msg
    assert "- nmap(target*:str, ports:str) — 端口扫描" in user_msg
    conn.close()


def test_plan_updates_stop_when_backlog_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
    rig: Callable[[list[object]], SimpleNamespace],
) -> None:
    """M5-30：active 规划超上限后本步新增不落账（audit 点名，提示先收口）。"""
    from secpt.core import loop as loop_mod

    monkeypatch.setattr(loop_mod, "_PLAN_OPEN_LIMIT", 1)

    def step(thought: str, updates: list[str], *, done: bool = False) -> str:
        return json.dumps(
            {
                "thought": thought,
                "plan_updates": updates,
                "plan_done": ["A：扫端口", "B：查证书"] if done else [],
                "tool_calls": [],
                "done": done,
            },
            ensure_ascii=False,
        )

    t = rig(
        [
            GOAL_JSON,
            step("先提两条", ["A：扫端口", "B：查证书"]),
            step("积压后再提一条", ["C：看 robots"]),
            step("收工", [], done=True),
        ]
    )
    result = t.loop.run("测试主机 10.0.0.5")

    assert result.exit_kind == "normal"
    rows = [
        row["content"]
        for row in t.conn.execute("SELECT content FROM plans ORDER BY rowid").fetchall()
    ]
    assert rows == ["A：扫端口", "B：查证书"]  # C 未落账
    audits = [str(row["what"]) for row in t.conn.execute("SELECT what FROM audit_log").fetchall()]
    assert any("规划积压" in what for what in audits)


def test_unfinished_receipt_counts_toward_stall() -> None:
    """未完工回执按"无进展"收数：连败数满即拉铃 E_STALL，不再被 exit=0 清零。"""
    conn = migrate.connect(":memory:")
    script: list[object] = [GOAL_JSON]
    script.extend(
        _step_json(
            f"空转{k}",
            calls=[{"name": "agent_run", "args": {"mission": "摸一圈"}}],
            done=False,
        )
        for k in range(4)
    )
    fake = FakeLLM(script)
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        NoProgressDispatcher(),
        exit_checker=ExitChecklist(),
        stall_failures=3,
    )
    with pytest.raises(SecptError) as info:
        loop.run("空转演示 10.0.0.5")
    assert info.value.code == "E_STALL"
    conn.close()


# ---- M5-31 重复申报拦截 / 重复调用拉铃 / 时长分解 ---------------------------------


def test_duplicate_declaration_is_intercepted(tmp_path) -> None:
    """同名 claimed 已在账：第二次申报不再新增行，audit 点名指回补证据。"""
    evidence = tmp_path / "ev.txt"
    evidence.write_text("22/tcp open ssh\n", encoding="utf-8")
    declare = {
        "name": "declare_finding",
        "args": {"title": "22 开放", "detail": "banner", "evidence_ref": str(evidence)},
    }
    fake = FakeLLM(
        [
            GOAL_JSON,
            _step_json("申报一次", calls=[declare], done=False),
            _step_json("按出场提示又申报一次", calls=[dict(declare)], done=False),
            _step_json("收工", calls=[], done=True),
        ]
    )
    from functools import partial
    from typing import cast

    from secpt.core.loop import JudgeSweep
    from secpt.judge import GroundTruth, JudgeProfile, sweep

    class DeclareDispatcher:
        """declare_finding 的 raw_ref 指向真实证据文件（与生产 DeclareFindingTool
        署名一致）——伪造路径会被 R0 判零成 rejected，走不到"verified 同锚重复"分支。"""

        def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
            raw = str(args.get("evidence_ref", "")) if name == "declare_finding" else "x"
            return ToolOutput(
                tool_name=name, args=dict(args), exit_code=0, summary="受理", raw_ref=raw
            )

    conn = migrate.connect(":memory:")
    prof = JudgeProfile(
        enabled=True,
        ground_truth=(GroundTruth(id="GT-22", pattern="22 开放"),),
        source="dedupe-test",
    )
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        DeclareDispatcher(),
        exit_checker=ExitChecklist(),
        judge_sweep=cast(JudgeSweep, partial(sweep, profile=prof)),
    )
    loop.run("去重演示 10.0.0.5")
    rows = conn.execute("SELECT title FROM findings").fetchall()
    assert len(rows) == 1  # 第二次同名申报未落行
    states = [str(r["verify_state"]) for r in conn.execute("SELECT verify_state FROM findings")]
    assert states == ["verified"]  # 唯一一条走裁判收口（oracle），出场方能过
    audits = [str(r["what"]) for r in conn.execute("SELECT what FROM audit_log").fetchall()]
    assert any("重复申报拦截" in what for what in audits)
    conn.close()


def test_repeat_calls_ring_fires_before_failure_streak() -> None:
    """同 (工具, 参数) 重复 4 次拉铃（repeat_calls=4 < failures=5）：空转在调用层
    被抓住，不必等失败计——真实 run 的"同 URL 反复抓/同文件反复读"正是此形态。"""

    class OkDispatcher:
        """永远成功的固定回执：失败计恒清零，只有重复计能响。"""

        def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
            return ToolOutput(
                tool_name=name, args=dict(args), exit_code=0, summary="ok", raw_ref="x"
            )

    conn = migrate.connect(":memory:")
    script = [GOAL_JSON]
    script.extend(
        _step_json(
            f"重读{k}",
            calls=[{"name": "read_file", "args": {"path": "raw/a.txt"}}],
            done=False,
        )
        for k in range(1, 5)
    )
    fake = FakeLLM(script)
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        OkDispatcher(),
        exit_checker=ExitChecklist(),
        stall_failures=5,
        stall_repeat_calls=4,
    )
    with pytest.raises(SecptError) as info:
        loop.run("重复调用演示 10.0.0.5")
    assert info.value.code == "E_STALL"
    assert fake.call_count == 5  # 进场 + 4 步（第 4 步撞线即出口）
    conn.close()


def test_run_ended_carries_duration_split() -> None:
    """run_ended 带 llm_ms/tools_ms：慢在决策还是慢在工具，收尾一行可辨。"""
    bus = EventBus()
    events: list[tuple[str, Mapping[str, object]]] = []
    bus.subscribe(lambda name, payload: events.append((name, payload)))
    conn = migrate.connect(":memory:")
    fake = FakeLLM([GOAL_JSON, _step_json("收工", calls=[], done=True)])
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        FakeDispatcher(),
        bus=bus,
        exit_checker=ExitChecklist(),
    )
    loop.run("计时演示 10.0.0.5")
    ended = dict(events[-1][1])
    assert "llm_ms" in ended and "tools_ms" in ended
    conn.close()
