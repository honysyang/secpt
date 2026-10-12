"""SteerBox：人工操舵消息队列（人在环路——不打断、不丢进度）。

ARTEX SteerWork 的可迁移实现：steer 消息写入 SQLite，worker 进程每步从
hooks.on_step 拉取注入为 user 消息（"【人工操舵】..."），模型下一轮即看到。
SQLite 而非内存/channel：跨进程可达（worker 是独立进程），且天然留痕审计。

配套：priority_intent（直投高优意图）写入 frontier 队列，见 frontier.py。
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("SECPT_VAR", "var")) / "steer.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS steer_msgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    msg TEXT NOT NULL,
    consumed INTEGER DEFAULT 0,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steer_job ON steer_msgs(job_id, consumed);
"""


class SteerBox:
    """人工操舵消息队列。best-effort，绝不抛给主链路。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def push(self, job_id: str, msg: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO steer_msgs (job_id,msg,ts) VALUES (?,?,?)",
                (job_id, msg[:1000], time.time()),
            )
            return int(cur.lastrowid)

    def pull(self, job_id: str) -> str | None:
        """取该 job 最早一条未消费消息并标记消费。无则 None。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT id,msg FROM steer_msgs WHERE job_id=? AND consumed=0 ORDER BY id LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            c.execute("UPDATE steer_msgs SET consumed=1 WHERE id=?", (row["id"],))
            return str(row["msg"])

    def pending(self, job_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM steer_msgs WHERE consumed=0"
        args: list = []
        if job_id:
            sql += " AND job_id=?"
            args.append(job_id)
        sql += " ORDER BY id"
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]


class SteerHook:
    """hooks 包装：on_step 时拉取 steer 消息返回给 Engine 注入。

    Engine 约定（runtime/engine.py）：hooks.on_step(steps, elapsed) 若返回
    非 None 字符串，将其 append 为 user 消息。__getattr__ 透传其余 hooks。
    """

    def __init__(self, inner, *, job_id: str, box: SteerBox | None = None) -> None:
        self._inner = inner
        self._job_id = job_id
        self._box = box or SteerBox()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def on_step(self, steps: int, elapsed: float):
        msg = None
        try:
            raw = self._box.pull(self._job_id)
            if raw:
                msg = f"【人工操舵】{raw}\n（这是操作员注入的提示，按此调整方向即可，不用回复确认）"
        except Exception:
            pass
        inner = getattr(self._inner, "on_step", None)
        if inner is not None:
            inner(steps, elapsed)
        return msg
