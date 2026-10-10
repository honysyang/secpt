"""SQLite GraphStore 测试。"""

from __future__ import annotations

import pytest

from secpt.config.models import GraphConfig
from secpt.core.graph import get_graph_store
from secpt.core.graph.sqlite import SQLiteGraphStore


@pytest.fixture
def store(tmp_path):
    """临时 SQLite store。"""
    s = SQLiteGraphStore(str(tmp_path / "test.db"))
    yield s
    # close is idempotent via try/finally in run(); here just ensure
    try:
        import asyncio
        asyncio.run(s.close())
    except Exception:
        pass


@pytest.mark.asyncio
async def test_sqlite_upsert_and_get(store: SQLiteGraphStore) -> None:
    await store.upsert_entity("host.1", "host", {"ip": "10.0.0.1"})
    ent = store.get_entity("host.1")
    assert ent is not None
    assert ent["etype"] == "host"
    assert ent["props"]["ip"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_sqlite_edge_and_neighbors(store: SQLiteGraphStore) -> None:
    await store.upsert_entity("task.1", "task", {})
    await store.upsert_entity("act.1.nmap", "action", {"tool": "nmap", "args": "-p 80 10.0.0.1"})
    await store.add_edge("task.1", "act.1.nmap", "performed")
    neighbors = await store.neighbors("task.1")
    assert len(neighbors) == 1
    assert neighbors[0]["eid"] == "act.1.nmap"
    assert neighbors[0]["edge_type"] == "performed"


@pytest.mark.asyncio
async def test_sqlite_evidence(store: SQLiteGraphStore) -> None:
    eid = await store.add_evidence("act.1.nmap", "80/tcp open  http", trust_tier="deterministic")
    assert eid
    ev = store.get_evidence(eid)
    assert ev is not None
    assert ev["payload"] == "80/tcp open  http"
    assert ev["trust_tier"] == "deterministic"
    assert ev["trust_score"] == 1.0


@pytest.mark.asyncio
async def test_sqlite_persistence_across_instances(tmp_path) -> None:
    """写入后关闭，新实例能读到。"""
    db_path = str(tmp_path / "persist.db")
    s1 = SQLiteGraphStore(db_path)
    await s1.upsert_entity("host.x", "host", {"ip": "1.2.3.4"})
    await s1.close()

    s2 = SQLiteGraphStore(db_path)
    ent = s2.get_entity("host.x")
    assert ent is not None
    assert ent["props"]["ip"] == "1.2.3.4"
    await s2.close()


def test_factory_sqlite_backend(tmp_path) -> None:
    cfg = GraphConfig(backend="sqlite", sqlite_path=str(tmp_path / "f.db"))
    store = get_graph_store(cfg)
    assert isinstance(store, SQLiteGraphStore)
