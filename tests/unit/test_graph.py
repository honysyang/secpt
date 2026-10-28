"""W2-2 双图读写 pilot 测试：v2 迁移、同键 upsert version 防漂移、摘要投影。"""

import sqlite3

import pytest

from secpt.errors import LedgerError
from secpt.llms.client import Goal
from secpt.store import connect, graph, schema_version
from secpt.store.attention import build
from secpt.store.ledger import start_task

TASK_ID = "t1"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    start_task(conn, "scan me", "get flag")
    yield conn
    conn.close()


class TestMigration:
    def test_v2_builds_graph_tables(self) -> None:
        fresh = connect(":memory:")
        # v2 只建双图；v3 补经验表——本测试仍锚 v2 的双图 DDL 是否在。
        assert schema_version(fresh) == 3
        tables = {r["name"] for r in fresh.execute("SELECT name FROM sqlite_master")}
        assert {"graph_nodes", "graph_edges"} <= tables
        fresh.close()


class TestNodes:
    def test_put_then_read(self, db: sqlite3.Connection) -> None:
        graph.put_node(db, TASK_ID, "asset", "10.0.0.1", {"open": [22]})
        rows = graph.task_nodes(db, TASK_ID)
        assert len(rows) == 1
        assert rows[0].kind == "asset"
        assert rows[0].key == "10.0.0.1"
        assert rows[0].props_json == '{"open": [22]}'
        assert rows[0].version == 1

    def test_upsert_bumps_version_and_updates_props(self, db: sqlite3.Connection) -> None:
        graph.put_node(db, TASK_ID, "asset", "10.0.0.1", {"open": [22]})
        again = graph.put_node(db, TASK_ID, "asset", "10.0.0.1", {"open": [22, 80]})
        assert again.version == 2
        assert again.props_json == '{"open": [22, 80]}'
        rows = graph.task_nodes(db, TASK_ID)
        assert len(rows) == 1  # 同键不滋生重复行
        assert rows[0] == again

    def test_kind_filter(self, db: sqlite3.Connection) -> None:
        graph.put_node(db, TASK_ID, "asset", "a", {})
        graph.put_node(db, TASK_ID, "step", "s", {})
        assert len(graph.task_nodes(db, TASK_ID, kind="asset")) == 1
        assert len(graph.task_nodes(db, TASK_ID)) == 2

    def test_rejects_bad_kind(self, db: sqlite3.Connection) -> None:
        with pytest.raises(LedgerError, match="kind"):
            graph.put_node(db, TASK_ID, "whatever", "k", {})


class TestEdges:
    def test_put_then_read(self, db: sqlite3.Connection) -> None:
        graph.put_edge(db, TASK_ID, "10.0.0.1", "10.0.0.2", "connects")
        rows = graph.task_edges(db, TASK_ID)
        assert len(rows) == 1
        assert rows[0].src == "10.0.0.1"
        assert rows[0].dst == "10.0.0.2"
        assert rows[0].version == 1

    def test_upsert_bumps_version(self, db: sqlite3.Connection) -> None:
        graph.put_edge(db, TASK_ID, "a", "b", "connects")
        again = graph.put_edge(db, TASK_ID, "a", "b", "connects")
        assert again.version == 2
        assert len(graph.task_edges(db, TASK_ID)) == 1


class TestSummary:
    def test_empty_graph_is_empty_string(self, db: sqlite3.Connection) -> None:
        assert graph.graph_summary(db, TASK_ID) == ""

    def test_counts_and_keys(self, db: sqlite3.Connection) -> None:
        graph.put_node(db, TASK_ID, "asset", "10.0.0.1", {})
        graph.put_node(db, TASK_ID, "asset", "10.0.0.2", {})
        graph.put_edge(db, TASK_ID, "10.0.0.1", "10.0.0.2", "connects")
        text = graph.graph_summary(db, TASK_ID)
        assert "asset=2" in text
        assert "边 1 条" in text
        assert "10.0.0.1" in text
        # 同键重复 put 不改变计数（version 防漂移的另一面：摘要不虚胖）
        graph.put_node(db, TASK_ID, "asset", "10.0.0.1", {"note": "up"})
        assert "asset=2" in graph.graph_summary(db, TASK_ID)

    def test_wires_into_attention_render(self, db: sqlite3.Connection) -> None:
        graph.put_node(db, TASK_ID, "asset", "10.0.0.1", {})
        goal = Goal(target="10.0.0.0/24", objective="get flag", success_criteria="1 verified")
        pkg = build(
            goal,
            step_no=1,
            plan_summary="",
            recent=[],
            graph_summary=graph.graph_summary(db, TASK_ID),
        )
        assert "## 双图摘要" in pkg.render()
