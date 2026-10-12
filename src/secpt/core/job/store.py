"""S1 SQLite 作业存储。

并发要点：
- PRAGMA journal_mode=WAL + busy_timeout=5000（多进程并发写不死锁）
- 每个操作独立开连接、短事务，线程安全（sqlite3 连接不跨线程复用）

事件流双写：events.jsonl（主）+ SQLite（次，失败不阻断）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from secpt.core.job.models import JobRecord, TaskSpec

JOB_STATUSES = {
    "queued",
    "running",
    "paused",
    "succeeded",
    "failed",
    "budget_exceeded",
    "step_exceeded",
    "wallclock_exceeded",
    "cancelled",
    "stuck",
}


def _ts() -> float:
    return datetime.now(UTC).timestamp()


class JobStore:
    """SQLite 作业/事件/finding 存取。db_path 默认 var/secpt.sqlite。"""

    def __init__(self, db_path: str | Path = "var/secpt.sqlite") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id     TEXT PRIMARY KEY,
                    spec_json  TEXT NOT NULL,
                    status     TEXT NOT NULL,
                    round      INTEGER NOT NULL DEFAULT 0,
                    steps      INTEGER NOT NULL DEFAULT 0,
                    cost_cny   REAL NOT NULL DEFAULT 0,
                    restarts   INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      TEXT NOT NULL,
                    ts          REAL NOT NULL,
                    round       INTEGER NOT NULL DEFAULT 0,
                    type        TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_job
                    ON events(job_id, seq);
                CREATE TABLE IF NOT EXISTS findings (
                    job_id   TEXT NOT NULL,
                    id       TEXT NOT NULL,
                    kind     TEXT NOT NULL,
                    value    TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    ts       REAL NOT NULL,
                    PRIMARY KEY (job_id, id)
                );
                """
            )
            # 老库补列（CREATE IF NOT EXISTS 不会给已存在的表加新列）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "restarts" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN restarts INTEGER NOT NULL DEFAULT 0")

    # ---------------- jobs ----------------

    def upsert_job(self, rec: JobRecord) -> None:
        if rec.status not in JOB_STATUSES:
            raise ValueError(f"invalid status: {rec.status!r}")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs
                       (job_id, spec_json, status, round, steps, cost_cny,
                        restarts, created_at, updated_at, last_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                       spec_json=excluded.spec_json, status=excluded.status,
                       round=excluded.round, steps=excluded.steps,
                       cost_cny=excluded.cost_cny, restarts=excluded.restarts,
                       updated_at=excluded.updated_at,
                       last_error=excluded.last_error""",
                (
                    rec.job_id,
                    rec.spec.model_dump_json(),
                    rec.status,
                    rec.round,
                    rec.steps,
                    rec.cost_cny,
                    rec.restarts,
                    rec.created_at,
                    rec.updated_at,
                    rec.last_error,
                ),
            )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[JobRecord]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def delete_job(self, job_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM events WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM findings WHERE job_id=?", (job_id,))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        spec = TaskSpec.model_validate_json(row["spec_json"])
        return JobRecord(
            job_id=row["job_id"],
            spec=spec,
            status=row["status"],
            round=row["round"],
            steps=row["steps"],
            cost_cny=row["cost_cny"],
            restarts=int(row["restarts"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )

    # ---------------- events ----------------

    def add_event(self, job_id: str, etype: str, payload: dict, round_: int = 0) -> int:
        """返回 SQLite seq。payload 必须是 JSON 可序列化。"""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO events (job_id, ts, round, type, payload_json) VALUES (?,?,?,?,?)",
                (job_id, _ts(), round_, etype, json.dumps(payload, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def search_traces(
        self, query: str, *, limit: int = 10, exclude_job: str | None = None
    ) -> list[dict]:
        """跨 job 过程级检索（ARTEX 原则 5：worker 互查执行过程）。

        在 events 表的 tool_result/assistant 内容里 LIKE 匹配，返回
        "哪个 job 哪一步见过类似响应"——线索先出现在中间日志里、
        未必已形成正式 fact 时，这是拼回完整画面的通道。
        """
        like = f"%{query[:80]}%"
        with self._conn() as conn:
            sql = (
                "SELECT job_id, seq, type, payload_json FROM events"
                " WHERE (type IN ('tool_result','assistant','finding'))"
                " AND payload_json LIKE ?"
            )
            args: list = [like]
            if exclude_job:
                sql += " AND job_id != ?"
                args.append(exclude_job)
            sql += " ORDER BY ts DESC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            preview = payload.get("preview") or payload.get("content") or payload.get("value") or ""
            out.append(
                {
                    "job_id": r["job_id"],
                    "seq": r["seq"],
                    "type": r["type"],
                    "preview": str(preview)[:200],
                }
            )
        return out

    def get_events(self, job_id: str, limit: int = 1000) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, ts, round, type, payload_json FROM events"
                " WHERE job_id=? ORDER BY seq DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        out = []
        for r in reversed(rows):
            out.append(
                {
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "round": r["round"],
                    "type": r["type"],
                    "payload": json.loads(r["payload_json"]),
                }
            )
        return out

    # ---------------- findings ----------------

    def add_finding(
        self,
        job_id: str,
        fid: str,
        kind: str,
        value: str,
        evidence: str = "",
        verified: bool = False,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO findings (job_id, id, kind, value, evidence, verified, ts)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(job_id, id) DO UPDATE SET
                       kind=excluded.kind, value=excluded.value,
                       evidence=excluded.evidence, verified=excluded.verified""",
                (job_id, fid, kind, value, evidence, int(verified), _ts()),
            )

    def list_findings(self, job_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, kind, value, evidence, verified, ts FROM findings"
                " WHERE job_id=? ORDER BY ts",
                (job_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "value": r["value"],
                "evidence": r["evidence"],
                "verified": bool(r["verified"]),
                "ts": r["ts"],
            }
            for r in rows
        ]
