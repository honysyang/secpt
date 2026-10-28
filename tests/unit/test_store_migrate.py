"""W1-1 事实层测试：迁移幂等 + 六表就位 + append-only 失败注入。"""

import sqlite3

import pytest

from secpt.errors import LedgerError
from secpt.store import connect, ensure_schema, schema_version

EXPECTED_TABLES = {
    "tasks",
    "runs",
    "plans",
    "steps",
    "tickets",
    "findings",
    "audit_log",
    "experience",
    "observations",
}


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    yield conn
    conn.close()


def _insert_task(conn: sqlite3.Connection, *, task_id: str = "t1") -> None:
    conn.execute(
        "INSERT INTO tasks (id, input_text, goal, mode, created_at)"
        " VALUES (?, 'scan me', 'get flag', 'pentest', 1)",
        (task_id,),
    )


class TestMigration:
    def test_baseline_builds_expected_tables(self, db: sqlite3.Connection) -> None:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {row["name"] for row in rows}
        assert EXPECTED_TABLES <= tables

        fresh = connect(":memory:")
        assert schema_version(fresh) == 4  # 1 baseline + 1 双图 + 1 经验库 + 1 观测流
        fresh.close()

    def test_migration_idempotent(self, db: sqlite3.Connection) -> None:
        # 重跑 N 次结果一致：版本不变、二次执行不抛错（幂等是迁移的红线）
        version_first = schema_version(db)
        for _ in range(3):
            ensure_schema(db)
        assert schema_version(db) == version_first
        _insert_task(db)
        db.commit()

    def test_connect_rejects_corrupt_file(self, tmp_path) -> None:
        bad = tmp_path / "corrupt.db"
        bad.write_bytes(b"this is not a sqlite database\x00\xff")
        with pytest.raises(LedgerError):
            connect(bad)


class _Actor:
    """W1-1 尚无上层写入口，直接用 SQL 驱动触发器断言。W1-3 起改走 store.ledger。"""


def test_append_only_tickets_update_rejected(db: sqlite3.Connection) -> None:
    _insert_task(db)
    db.execute(
        "INSERT INTO runs (id, task_id, config_json, started_at) VALUES ('r1', 't1', '{}', 1)"
    )
    db.execute(
        "INSERT INTO tickets (id, task_id, run_id, step_id, tool_name, args_json,"
        " summary, raw_ref, created_at)"
        " VALUES ('k1', 't1', 'r1', 's1', 'nmap', '{}', 'done', 'out.txt', 2)"
    )
    db.commit()

    with pytest.raises(sqlite3.Error, match="E_APPEND_ONLY"):
        db.execute("UPDATE tickets SET summary = 'tampered' WHERE id = 'k1'")  # type: ignore[union-attr]
    with pytest.raises(sqlite3.Error, match="E_APPEND_ONLY"):
        db.execute("DELETE FROM tickets WHERE id = 'k1'")  # type: ignore[union-attr]

    # 被拒绝的语句外的事务语句仍可提交（RAISE(ABORT) 只中止当前语句）
    db.rollback()


def test_append_only_findings_delete_rejected(db: sqlite3.Connection) -> None:
    _insert_task(db)
    db.execute(
        "INSERT INTO findings (id, task_id, title, detail, evidence_ref, created_at)"
        " VALUES ('f1', 't1', 'sqli', 'detail', 'tickets:k1', 3)"
    )
    db.commit()

    with pytest.raises(sqlite3.Error, match="E_APPEND_ONLY"):
        db.execute("DELETE FROM findings WHERE id = 'f1'")  # type: ignore[union-attr]
    db.rollback()

    # findings 的 UPDATE 是合法路径（裁判要推进 verify_state）
    db.execute("UPDATE findings SET verify_state = 'verified' WHERE id = 'f1'")
    db.commit()
    state = db.execute("SELECT verify_state FROM findings WHERE id = 'f1'").fetchone()[0]
    assert state == "verified"


def test_append_only_audit_log_guarded(db: sqlite3.Connection) -> None:
    _insert_task(db)
    db.execute(
        "INSERT INTO audit_log (task_id, actor, what, created_at) VALUES ('t1', 'main', 'run', 1)"
    )
    db.commit()
    with pytest.raises(sqlite3.Error, match="E_APPEND_ONLY"):
        db.execute("UPDATE audit_log SET what = 'rewritten'")  # type: ignore[union-attr]
    db.rollback()


def test_check_constraints_on_enums(db: sqlite3.Connection) -> None:
    _insert_task(db)
    db.commit()
    with pytest.raises(sqlite3.Error):
        db.execute(
            "INSERT INTO runs (id, task_id, status, config_json, started_at)"
            " VALUES ('r2', 't1', 'weird', '{}', 1)"
        )
    db.rollback()


def test_foreign_keys_enforced(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.Error):
        db.execute(
            "INSERT INTO runs (id, task_id, config_json, started_at)"
            " VALUES ('r3', 'missing-task', '{}', 1)"
        )
