"""记忆存储单元测试。"""

import pytest

from harness.memory.store import InMemoryMemory, MemoryRecord, SQLiteMemory


class TestInMemoryMemory:
    @pytest.fixture
    def store(self):
        return InMemoryMemory()

    def test_remember_and_recall(self, store):
        rec = MemoryRecord(key="name", content="Alice", kind="fact")
        store.remember(rec)
        assert store.recall("name").content == "Alice"

    def test_recall_missing(self, store):
        assert store.recall("nonexistent") is None

    def test_forget(self, store):
        store.remember(MemoryRecord(key="temp", content="x"))
        assert store.forget("temp") is True
        assert store.recall("temp") is None

    def test_forget_missing(self, store):
        assert store.forget("nonexistent") is False

    def test_namespace_isolation(self, store):
        store.remember(MemoryRecord(key="k", content="v1", namespace="ns1"))
        store.remember(MemoryRecord(key="k", content="v2", namespace="ns2"))
        assert store.recall("k", "ns1").content == "v1"
        assert store.recall("k", "ns2").content == "v2"


class TestSQLiteMemory:
    @pytest.fixture
    def store(self, tmp_path):
        # 检查 sqlite3 是否可用
        try:
            import sqlite3
        except ImportError:
            pytest.skip("sqlite3 not available")

        db_path = tmp_path / "test_memory.db"
        store = SQLiteMemory(str(db_path))
        yield store
        store.close()

    def test_remember_and_recall(self, store):
        rec = MemoryRecord(key="name", content="Alice", kind="fact")
        store.remember(rec)
        assert store.recall("name").content == "Alice"

    def test_recall_missing(self, store):
        assert store.recall("nonexistent") is None

    def test_forget(self, store):
        store.remember(MemoryRecord(key="temp", content="x"))
        assert store.forget("temp") is True
        assert store.recall("temp") is None

    def test_forget_missing(self, store):
        assert store.forget("nonexistent") is False

    def test_namespace_isolation(self, store):
        store.remember(MemoryRecord(key="k", content="v1", namespace="ns1"))
        store.remember(MemoryRecord(key="k", content="v2", namespace="ns2"))
        assert store.recall("k", "ns1").content == "v1"
        assert store.recall("k", "ns2").content == "v2"

    def test_list_all(self, store):
        store.remember(MemoryRecord(key="a", content="1", namespace="ns"))
        store.remember(MemoryRecord(key="b", content="2", namespace="ns"))
        store.remember(MemoryRecord(key="c", content="3", namespace="other"))

        results = store.list_all("ns")
        assert len(results) == 2

    def test_search(self, store):
        store.remember(MemoryRecord(key="k1", content="Python programming"))
        store.remember(MemoryRecord(key="k2", content="JavaScript coding"))
        store.remember(MemoryRecord(key="k3", content="Python data science"))

        results = store.search("Python")
        assert len(results) == 2

    def test_persistence(self, store, tmp_path):
        """测试持久化：写入后重新打开能读到数据。"""
        store.remember(MemoryRecord(key="persist", content="value"))

        # 重新打开
        db_path = tmp_path / "test_memory.db"
        store2 = SQLiteMemory(str(db_path))
        assert store2.recall("persist").content == "value"
        store2.close()

    def test_metadata(self, store):
        rec = MemoryRecord(
            key="meta",
            content="test",
            metadata={"source": "test", "confidence": 0.95},
        )
        store.remember(rec)
        result = store.recall("meta")
        assert result.metadata["source"] == "test"
        assert result.metadata["confidence"] == 0.95
