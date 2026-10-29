"""M5-15 双图：证据图写入 / 双图补齐 / 出场①"小票/双图一致"。

口径：
- 探索图 = step 链（M4-6）；证据图 = finding 实体 + evidences 边（本卡）；
- 主循环/worker 落 finding 时同步写节点；进场续跑前补历史缺口；
- 出场清单①判一致，但以"图上已有该类节点"为闸（老库不倒判）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from secpt.core.loop import PentestLoop
from secpt.finalize.exit_checklist import ExitChecklist, run_checklist
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import graph, ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import DeclareFindingTool
from secpt.tools.registry import ToolRegistry


def _goal_json() -> str:
    return json.dumps(
        {"target": "10.0.0.5", "objective": "o", "success_criteria": "s"}, ensure_ascii=False
    )


def _step_json(thought: str, *, calls: list[dict[str, object]] | None = None, done: bool) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": calls or [], "done": done},
        ensure_ascii=False,
    )


def _declare(title: str, evidence_ref: str) -> dict[str, object]:
    return {
        "name": "declare_finding",
        "args": {"title": title, "detail": "有凭据", "evidence_ref": evidence_ref},
    }


def _task_with_run(conn: sqlite3.Connection) -> tuple[str, object]:
    task = ledger.start_task(conn, "双图演示", goal="10.0.0.5")
    run = ledger.start_run(conn, task.id, config={"objective": "o", "success_criteria": "s"})
    return task.id, run


# ---------- 补齐器 --------------------------------------------------------------


class TestEnsureNodes:
    def test_ensure_finding_nodes_is_idempotent(self) -> None:
        conn = migrate_store.connect(":memory:")
        task = ledger.start_task(conn, "补齐演示", goal="g")
        finding = ledger.append_finding(conn, task.id, "开放端口 22", "d", "raw/a.txt")

        assert graph.ensure_finding_nodes(conn, task.id) == 1
        assert graph.ensure_finding_nodes(conn, task.id) == 0  # 幂等
        keys = [
            row["key"]
            for row in conn.execute("SELECT key FROM graph_nodes WHERE kind = 'finding'").fetchall()
        ]
        assert keys == [f"finding-{finding.id}"]

    def test_ensure_step_nodes_backfills_thought(self) -> None:
        conn = migrate_store.connect(":memory:")
        task_id, run = _task_with_run(conn)
        ledger.record_step(conn, task_id, run.id, 1, _step_json("断电前的思考", done=False))

        assert graph.ensure_step_nodes(conn, task_id) == 1
        assert graph.ensure_step_nodes(conn, task_id) == 0
        props = conn.execute(
            "SELECT props_json FROM graph_nodes WHERE kind = 'step' AND key = 'step-1'"
        ).fetchone()["props_json"]
        assert "断电前的思考" in props and "backfilled" in props


# ---------- 主循环写证据图 ------------------------------------------------------


def test_loop_writes_evidence_graph(tmp_path: Path) -> None:
    """declare_finding 落账时同事务写 finding 节点 + evidences 边。"""
    evidence = tmp_path / "ev.txt"
    evidence.write_text("22/tcp open ssh\n", encoding="utf-8")
    fake = FakeLLM(
        [
            _goal_json(),
            _step_json("申报", calls=[_declare("22 开放", str(evidence))], done=False),
        ]
    )
    conn = migrate_store.connect(":memory:")
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        ToolRegistry([DeclareFindingTool()]),
        exit_checker=ExitChecklist(),
    )
    loop.run("证据图演示 10.0.0.5", max_steps=1)

    nodes = conn.execute("SELECT kind, key FROM graph_nodes ORDER BY kind, key").fetchall()
    kinds = {row["kind"] for row in nodes}
    assert kinds == {"step", "finding"}
    edges = conn.execute("SELECT src, dst, kind FROM graph_edges").fetchall()
    assert len(edges) == 1
    assert edges[0]["src"] == "step-1" and edges[0]["kind"] == "evidences"
    assert edges[0]["dst"].startswith("finding-")
    audit = conn.execute(
        "SELECT what FROM audit_log WHERE actor = 'main' AND what LIKE 'step 1:%'"
    ).fetchone()["what"]
    assert "graph=+1n+1e" in audit  # 1 节点 + 1 证据边


# ---------- 出场①一致性 --------------------------------------------------------


class TestExitGraphConsistency:
    def test_step_node_gap_blocks_exit(self) -> None:
        conn = migrate_store.connect(":memory:")
        task_id, run = _task_with_run(conn)
        ledger.record_step(conn, task_id, run.id, 1, "{}")
        ledger.record_step(conn, task_id, run.id, 2, "{}")
        graph.put_node(conn, task_id, "step", "step-1", {"thought": "只写了一步"})

        verdict = run_checklist(conn, task_id)
        assert verdict.passed is False
        assert any("探索图不一致" in f for f in verdict.failures)

    def test_verified_finding_without_node_blocks_exit(self) -> None:
        conn = migrate_store.connect(":memory:")
        task_id, run = _task_with_run(conn)
        ledger.record_step(conn, task_id, run.id, 1, "{}")
        graph.put_node(conn, task_id, "step", "step-1", {"thought": "ok"})
        graph.put_node(conn, task_id, "finding", "finding-other", {"title": "别的"})
        ledger.append_finding(
            conn, task_id, "未入图的 verified", "d", "raw/a.txt", verify_state="verified"
        )

        verdict = run_checklist(conn, task_id)
        assert verdict.passed is False
        assert any("证据图不一致" in f for f in verdict.failures)

    def test_legacy_ledger_without_nodes_not_judged(self) -> None:
        """老库（完全没有图节点）不倒判：图是引擎产物，空图说明不是引擎写的。"""
        conn = migrate_store.connect(":memory:")
        task_id, run = _task_with_run(conn)
        ledger.record_step(conn, task_id, run.id, 1, "{}")
        ledger.append_finding(
            conn, task_id, "历史 verified", "d", "raw/a.txt", verify_state="verified"
        )
        assert run_checklist(conn, task_id).passed is True


def test_voided_step_blocks_exit() -> None:
    """步骤未全部完成（M5-19）：账上留 voided 步 = 本 run 不完整，不许正常出场。"""
    conn = migrate_store.connect(":memory:")
    task_id, run = _task_with_run(conn)
    ledger.record_step(conn, task_id, run.id, 1, "{}", status="voided")

    verdict = run_checklist(conn, task_id)
    assert verdict.passed is False
    assert any("步骤未全部完成" in f for f in verdict.failures)
