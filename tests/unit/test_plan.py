"""M5-5 规划对象测试：plan_updates 落账 / 注意力包回读 / 教练修订 / 续跑延续。

口径（设计文档 L9-10 / L16）：
- 主 LLM 的 plan_updates 是账本事实（plans 表 + steps.plan_refs），不是内存变量；
- 注意力包每步从账本渲染"规划 v<修订号>（active N 条）+ 逐条"，resume 后自然延续；
- 教练提案（救场/重估）作为规划修订落账（parent_id 指向上版）；
- 去重：复述同一规划不重复落行；空 updates 不写账。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

import pytest

from secpt.core.loop import PentestLoop
from secpt.core.plan_watch import PlanWatcher
from secpt.errors import SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import LlmClient
from secpt.llms.coach import Coach, Proposal
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.ob.events import EventBus
from secpt.store import ledger, plan
from secpt.store import migrate as migrate_store
from secpt.tools.registry import ToolRegistry


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = migrate_store.connect(":memory:")
    yield c
    c.close()


# ---------- store/plan 器官 -----------------------------------------------------


class TestPlanStore:
    def test_add_and_render(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "规划演示", goal="g").id
        rows = plan.add(conn, task_id, 1, ["A：端口摸底", " B：web 初筛 ", ""])
        assert [row.content for row in rows] == ["A：端口摸底", "B：web 初筛"]
        assert {row.version for row in rows} == {1}
        text = plan.render(conn, task_id)
        assert "规划 v1（active 2 条）" in text
        assert "- [id=" in text and "· step 1] A：端口摸底" in text

    def test_duplicate_content_is_not_appended(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "去重演示", goal="g").id
        plan.add(conn, task_id, 1, ["A：端口摸底"])
        assert plan.add(conn, task_id, 2, ["A：端口摸底"]) == []
        assert len(plan.active(conn, task_id)) == 1

    def test_new_batch_bumps_version(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "版本演示", goal="g").id
        plan.add(conn, task_id, 1, ["A"])
        rows = plan.add(conn, task_id, 2, ["B"], parent_id=plan.latest_id(conn, task_id))
        assert len(rows) == 1 and rows[0].version == 2 and rows[0].parent_id is not None
        assert "规划 v2（active 2 条）" in plan.render(conn, task_id)

    def test_empty_input_is_noop(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "空演示", goal="g").id
        assert plan.add(conn, task_id, 1, []) == []
        assert plan.add(conn, task_id, 1, ["   "]) == []
        assert plan.render(conn, task_id).startswith("（暂无规划")

    def test_complete_by_content_and_cancel(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "收口演示", goal="g").id
        plan.add(conn, task_id, 1, ["A：摸底", "B：初筛"])
        done, unresolved = plan.complete(conn, task_id, ["A：摸底"])
        assert [row.status for row in done] == ["done"] and unresolved == []
        cancelled, unresolved = plan.complete(conn, task_id, ["B：初筛"], status="cancelled")
        assert [row.status for row in cancelled] == ["cancelled"] and unresolved == []
        assert plan.active(conn, task_id) == []
        assert "暂无规划" in plan.render(conn, task_id)

    def test_complete_by_unique_id_prefix(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "前缀演示", goal="g").id
        row = plan.add(conn, task_id, 1, ["A：摸底"])[0]
        done, unresolved = plan.complete(conn, task_id, [row.id[:8]])
        assert [item.id for item in done] == [row.id] and unresolved == []
        # 已达到终态：重复收口不算命中（只认 active），如实报未匹配。
        assert plan.complete(conn, task_id, [row.id[:8]]) == ([], [row.id[:8]])

    def test_complete_never_guesses(self, conn: sqlite3.Connection) -> None:
        """歧义/过短/不存在的引用一律进 unresolved，绝不误收口。"""
        task_id = ledger.start_task(conn, "不猜演示", goal="g").id
        plan.add(conn, task_id, 1, ["A：摸底", "B：初筛"])
        done, unresolved = plan.complete(conn, task_id, ["幽灵规划", "abc"])
        assert done == [] and unresolved == ["幽灵规划", "abc"]
        assert len(plan.active(conn, task_id)) == 2

    def test_complete_rejects_bad_status(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "坏状态", goal="g").id
        with pytest.raises(ValueError):
            plan.complete(conn, task_id, ["x"], status="failed")


# ---------- 主循环接线 ----------------------------------------------------------


def _goal_json() -> str:
    return json.dumps(
        {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"}, ensure_ascii=False
    )


def _step_json(
    thought: str,
    *,
    plan_updates: list[str] | None = None,
    plan_done: list[str] | None = None,
    plan_cancel: list[str] | None = None,
    done: bool = False,
) -> str:
    return json.dumps(
        {
            "thought": thought,
            "plan_updates": plan_updates or [],
            "plan_done": plan_done or [],
            "plan_cancel": plan_cancel or [],
            "tool_calls": [],
            "done": done,
        },
        ensure_ascii=False,
    )


def _loop(
    fake: FakeLLM, *, watcher: PlanWatcher | None = None, conn: sqlite3.Connection | None = None
) -> tuple[PentestLoop, EventBus, list[tuple[str, Mapping[str, object]]], sqlite3.Connection]:
    conn = conn or migrate_store.connect(":memory:")
    bus = EventBus()
    events: list[tuple[str, Mapping[str, object]]] = []
    bus.subscribe(lambda name, payload: events.append((name, payload)))
    if watcher is not None:
        bus.subscribe(watcher)
    client = LlmClient(fake)
    loop = PentestLoop(
        conn,
        MainLlm(client),
        ToolRegistry([]),
        bus=bus,
        exit_checker=ExitChecklist(),
        coach=Coach(client),
        plan_watcher=watcher,
    )
    return loop, bus, events, conn


def test_plan_updates_land_in_ledger_and_next_pack() -> None:
    """第一步提出 A/B，第二步的注意力包就能读到同一份规划（账本事实，非内存）。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("先摸端口", plan_updates=["A：端口摸底", "B：web 初筛"]),
            _step_json("收工", plan_done=["A：端口摸底", "B：web 初筛"], done=True),
        ]
    )
    loop, _bus, _events, conn = _loop(fake)
    loop.run("规划落地演示 10.0.0.5")

    task = ledger.latest_task(conn)
    rows = conn.execute(
        "SELECT content, step_no, status FROM plans WHERE task_id = ? ORDER BY rowid",
        (task.id,),
    ).fetchall()
    assert [row["content"] for row in rows] == ["A：端口摸底", "B：web 初筛"]
    assert {row["status"] for row in rows} == {"done"}  # 收工步已一并收口
    assert [row["step_no"] for row in rows] == [1, 1]

    second_pack = fake.calls[2][1].content
    assert "规划 v1（active 2 条）" in second_pack
    assert "· step 1] A：端口摸底" in second_pack  # 模型下一步看到的是真实规划

    step_rows = conn.execute(
        "SELECT plan_refs FROM steps WHERE task_id = ? ORDER BY step_no", (task.id,)
    ).fetchall()
    refs = json.loads(step_rows[0]["plan_refs"])
    assert len(refs) == 2 and json.loads(step_rows[1]["plan_refs"]) == []
    audits = [r["what"] for r in conn.execute("SELECT what FROM audit_log WHERE actor='main'")]
    assert any("plan=+2" in what for what in audits)


def test_plan_survives_resume() -> None:
    """断电续跑：规划不靠内存，resume 后第一包仍读得到原规划。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "续跑规划演示", goal="10.0.0.5")
    run = ledger.start_run(conn, task.id, config={"objective": "o", "success_criteria": "s"})
    ledger.record_step(conn, task.id, run.id, 1, "{}")
    plan.add(conn, task.id, 1, ["A：端口摸底"])
    ledger.mark_step(conn, run.id, 1)

    fake = FakeLLM([_step_json("续走一步", plan_done=["A：端口摸底"], done=True)])
    loop, _bus, _events, _conn = _loop(fake, conn=conn)
    loop.resume(task.id, max_steps=2)
    assert "· step 1] A：端口摸底" in fake.calls[0][1].content
    assert ledger.latest_task(conn).exit_kind == "normal"


def test_coach_proposal_becomes_plan_revision() -> None:
    """教练提案落成规划修订（version+1、parent 指向上版），不只是临时提示。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("先摸端口", plan_updates=["A：端口摸底"]),
            Proposal(idea="B：换打 443", reason="22 没动静"),
            _step_json("按教练推进", plan_done=["A：端口摸底", "B：换打 443"], done=True),
        ]
    )
    loop, _bus, _events, conn = _loop(fake, watcher=PlanWatcher(every_steps=1))
    loop.run("教练规划演示 10.0.0.5")

    task_id = ledger.latest_task(conn).id
    rows = plan_rows(conn, task_id)
    contents = [row.content for row in rows]
    assert "B：换打 443" in contents
    revision = next(row for row in rows if row.content == "B：换打 443")
    first = next(row for row in rows if row.content == "A：端口摸底")
    assert revision.version == first.version + 1
    assert revision.parent_id is not None


def plan_rows(conn: sqlite3.Connection, task_id: str) -> list[plan.PlanRow]:
    """全量规划行（含终态）：收口后 active 为空，断言要看历史全貌。"""
    rows = conn.execute(
        "SELECT * FROM plans WHERE task_id = ? ORDER BY rowid", (task_id,)
    ).fetchall()
    return [plan.PlanRow.model_validate(dict(row)) for row in rows]


def test_open_plan_blocks_exit_until_closed() -> None:
    """规划未收口：done 连败 3 轮按 E_WALL 走异常出口（"规划 A/B/C"不许只说不收）。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("先摸端口", plan_updates=["A：端口摸底"]),
            _step_json("收工", done=True),
            _step_json("收工", done=True),
            _step_json("收工", done=True),
        ]
    )
    loop, _bus, _events, conn = _loop(fake)
    with pytest.raises(SecptError) as info:
        loop.run("规划未收口演示 10.0.0.5")
    assert info.value.code == "E_WALL"
    assert ledger.latest_task(conn).exit_kind == "stalled"


def test_unresolved_plan_ref_is_audited() -> None:
    """写了不存在的规划引用：不猜、不改状态，但必须落审计点名。"""
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("先摸端口", plan_updates=["A：端口摸底"]),
            _step_json(
                "收工",
                plan_done=["A：端口摸底", "幽灵规划"],
                done=True,
            ),
        ]
    )
    loop, _bus, _events, conn = _loop(fake)
    loop.run("未匹配演示 10.0.0.5")

    task_id = ledger.latest_task(conn).id
    assert ledger.latest_task(conn).exit_kind == "normal"
    audits = [
        r["what"] for r in conn.execute("SELECT what FROM audit_log WHERE task_id=?", (task_id,))
    ]
    assert any("规划引用未匹配" in what and "幽灵规划" in what for what in audits)
    statuses = [
        row["status"]
        for row in conn.execute("SELECT status FROM plans WHERE task_id=?", (task_id,))
    ]
    assert statuses == ["done"]


# ---- M5-32 引用剥包装：模型照抄渲染行 "[id=…] 原文" 也能收口 ------------------------


def test_complete_accepts_rendered_line_wrapper() -> None:
    """plan_done 引用带 "[id=xxxxxxxx · step N] 原文" 包装：剥掉后按 id/原文各试
    一次——实测一轮 16 次收口引用有 3 条因带包装全部打空，20 条 active 堆到出场。"""
    conn = migrate_store.connect(":memory:")
    task_id = ledger.start_task(conn, "t", "g").id
    (a, b) = plan.add(conn, task_id, 1, ["A：扫端口", "B：查证书"])
    for row, status in ((a, "done"), (b, "cancelled")):
        wrapped = f"[id={row.id[:8]} · step 3] {row.content}"
        done_rows, unresolved = plan.complete(conn, task_id, [wrapped], status=status)
        assert done_rows and not unresolved  # 两种包装都能解析
    states = {r["content"]: r["status"] for r in conn.execute("SELECT content, status FROM plans")}
    assert states == {"A：扫端口": "done", "B：查证书": "cancelled"}


def test_complete_accepts_bare_id_bracket_wrapper() -> None:
    """只带 "[id=xxxxxxxx]" 不带原文的引用同样收口。"""
    conn = migrate_store.connect(":memory:")
    task_id = ledger.start_task(conn, "t", "g").id
    (row,) = plan.add(conn, task_id, 1, ["A：扫端口"])
    done_rows, unresolved = plan.complete(conn, task_id, [f"[id={row.id[:8]}]"])
    assert done_rows and not unresolved
    assert conn.execute("SELECT status FROM plans").fetchone()["status"] == "done"
