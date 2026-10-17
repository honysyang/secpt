"""跨进程互斥账本：challenge 状态机 + 容器租约（P1）。

治"跨 worker 重复解题"（tsec-run-0522：g-05 被 3 个 worker 各解一遍）。
多个 worker 进程（3 worker × N 题）共享同一 SQLite 账本，claim 时原子 CAS：
只有本进程"赢下"的 challenge 才会派发执行，其余跳过（省容器额度与步数）。

状态机：queued → claimed → running → solved / exhausted / failed
租约：claim 时写 lease_until（进程内心跳续租），超时未释放 → 自动回 queued
      （进程崩溃 / 被 kill 后，账本不永久卡死，其他 worker 可接棒）。
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS challenges (
    unique_code TEXT PRIMARY KEY,
    status TEXT DEFAULT 'queued',       -- queued / claimed / running / solved / exhausted / failed
    worker_id TEXT DEFAULT '',           -- 持有租约的 worker 标识（pid@host）
    lease_until REAL DEFAULT 0.0,        -- 租约到期时间（epoch）；到期未续 → 自动回 queued
    flags_found INTEGER DEFAULT 0,       -- 已 submit 的 flag 数（跑分断点）
    flags_total INTEGER DEFAULT 0,
    solved_at REAL DEFAULT 0.0,
    last_error TEXT DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_challenges_status ON challenges(status);
CREATE TABLE IF NOT EXISTS scope_done (
    scope_key TEXT PRIMARY KEY,        -- 资产/题目维度的完成广播键（跨进程共享）
    done_by TEXT DEFAULT '',           -- 置位者（worker id，追溯用）
    done_at REAL NOT NULL
);
"""

WORKER_ID = f"{os.getpid()}@{socket.gethostname()}"  # worker 标识（pid@host，去重键）


def _lease_holder_alive(worker_id: str) -> bool:
    """租约持有者是否仍存活（解析 pid@host，同主机才可判定）。

    跨主机/解析失败 → 保守视为存活（不误放行）；同主机 pid 不存在 → 已死
    （崩溃残留租约不该按 15min TTL 阻塞重启）。
    """
    try:
        pid_s, host = worker_id.split("@", 1)
        if host != socket.gethostname():
            return True
        os.kill(int(pid_s), 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, PermissionError, OSError):
        return True


LEASE_TTL_S = 900.0  # 租约时长（15 分钟，够一题硬止损 25 步）；心跳每 60s 续租


class ChallengeLedger:
    """跨进程 challenge 账本（SQLite WAL 并发读写，CAS claim 防重复）。"""

    def __init__(self, db_path: Path | str | None = None, *, worker_id: str = "") -> None:
        # 默认库按调用时 SECPT_VAR 解析（模块级 DEFAULT_LEDGER 是 import 期快照，
        # 测试/多实例会漂移——与 frontier/findings 的取值口径一致）
        if db_path:
            self.db_path = Path(db_path)
        else:
            var = Path(os.environ.get("SECPT_VAR", "var"))
            # BENCHMARK_TASK_ID 设置时账本按任务隔离——新跑分任务复用同题编码（g-*）时，
            # 旧任务的 solved/exhausted 断点不会误当本任务的进度（无该 env 时保持旧路径）
            task = os.environ.get("BENCHMARK_TASK_ID", "").strip()
            name = f"benchmark_ledger_{task}.sqlite" if task else "benchmark_ledger.sqlite"
            self.db_path = var / name
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_id = worker_id or WORKER_ID
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # 并发读写（多 worker 进程共享）
        return conn

    def _migrate(self, c: sqlite3.Connection) -> None:
        """老库补列。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(challenges)")}
        for name, decl in (
            ("flags_total", "INTEGER DEFAULT 0"),
            ("last_error", "TEXT DEFAULT ''"),
        ):
            if name not in cols:
                with contextlib.suppress(sqlite3.Error):
                    c.execute(f"ALTER TABLE challenges ADD COLUMN {name} {decl}")

    # ---------------- 状态机操作 ----------------

    def ensure(self, unique_code: str, *, flags_total: int = 1) -> None:
        """登记 challenge（幂等）：已存在则不改。"""
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO challenges (unique_code, flags_total, updated_at)"
                " VALUES (?,?,?)",
                (unique_code, flags_total, now),
            )

    def claim(self, unique_code: str, *, lease_ttl_s: float = LEASE_TTL_S) -> bool:
        """原子认领（CAS）：queued 或租约过期 → 归属本 worker；已被他人持有 → False。

        - 只有本进程"赢下"的 challenge 才执行（防跨 worker 重复解题）
        - 租约到期未续 → 自动回 queued，其他 worker 可接棒（进程崩溃不永久卡死）
        """
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """UPDATE challenges
                   SET status='claimed', worker_id=?, lease_until=?, updated_at=?
                   WHERE unique_code=?
                     AND (status IN ('queued','failed','exhausted')
                          OR (status IN ('claimed','running') AND lease_until < ?))""",
                (self.worker_id, now + lease_ttl_s, now, unique_code, now),
            )
            return cur.rowcount > 0

    def heartbeat(self, unique_code: str, *, lease_ttl_s: float = LEASE_TTL_S) -> bool:
        """续租：只有本 worker 持有租约才续（其他 worker 抢占后心跳不生效）。"""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE challenges SET lease_until=?, updated_at=?"
                " WHERE unique_code=? AND worker_id=? AND status IN ('claimed','running')",
                (now + lease_ttl_s, now, unique_code, self.worker_id),
            )
            return cur.rowcount > 0

    def mark_running(self, unique_code: str) -> None:
        """已认领的 challenge 开始执行（进入 running 状态）。"""
        with self._conn() as c:
            c.execute(
                "UPDATE challenges SET status='running', updated_at=?"
                " WHERE unique_code=? AND worker_id=?",
                (time.time(), unique_code, self.worker_id),
            )

    def mark_solved(self, unique_code: str, *, flags_found: int = 0, flags_total: int = 0) -> None:
        """结算：拿到 flag / 平台确认完成。"""
        now = time.time()
        with self._conn() as c:
            c.execute(
                """UPDATE challenges
                   SET status='solved', worker_id='', lease_until=0,
                       flags_found=?, flags_total=?, solved_at=?, updated_at=?
                   WHERE unique_code=?""",
                (flags_found, flags_total or flags_found, now, now, unique_code),
            )

    def mark_failed(self, unique_code: str, reason: str = "") -> None:
        """结算：步数耗尽 / 不可解（下次可重试，claim 会重新认领）。"""
        with self._conn() as c:
            c.execute(
                "UPDATE challenges SET status='exhausted', worker_id='', lease_until=0,"
                " last_error=?, updated_at=? WHERE unique_code=?",
                (reason[:200], time.time(), unique_code),
            )

    def set_flags_found(self, unique_code: str, flags_found: int) -> None:
        """部分旗进度回填（不改变状态机状态）：下轮从缺口续打。"""
        with self._conn() as c:
            c.execute(
                "UPDATE challenges SET flags_found=?, updated_at=? WHERE unique_code=?",
                (int(flags_found), time.time(), unique_code),
            )

    def iter_entries(self) -> list[dict]:
        """全量账本行（对账用）。"""
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM challenges").fetchall()]

    def solved_codes(self) -> list[str]:
        """当前 solved 的题号列表（对账用）。"""
        with self._conn() as c:
            return [
                str(r[0])
                for r in c.execute(
                    "SELECT unique_code FROM challenges WHERE status='solved'"
                ).fetchall()
            ]

    def release(self, unique_code: str) -> None:
        """释放租约（不结算）：回到 queued，其他 worker 可接棒。"""
        with self._conn() as c:
            c.execute(
                "UPDATE challenges SET status='queued', worker_id='', lease_until=0, updated_at=?"
                " WHERE unique_code=? AND worker_id=?",
                (time.time(), unique_code, self.worker_id),
            )

    def release_own_claims(self, *, reason: str = "") -> int:
        """A2 轮间释放：只释放本 worker 的 claimed/running → exhausted（可重领）。

        与 release_all_claims 的区别：不触碰其他存活 worker 的租约——campaign
        多轮重启当下进程的 claim，即便有并发实例也不会互相踩。solved 永不触碰。
        """
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE challenges SET status='exhausted', worker_id='', lease_until=0,"
                " last_error=?, updated_at=?"
                " WHERE worker_id=? AND status IN ('claimed','running')",
                (reason[:200] or "轮间释放：campaign 下一轮重试", now, self.worker_id),
            )
            return int(cur.rowcount or 0)

    def release_all_claims(self, *, reason: str = "") -> int:
        """运维操作：强制释放全部 claimed/running 租约（回 exhausted 可重试）。

        只用于"跑分进程已死/将重启"场景（调用方负责确认无存活 worker）——
        正常路径租约由心跳续期、超时自动回 queued，不需要本方法。
        返回释放条数。 solved 行永不触碰。
        """
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE challenges SET status='exhausted', worker_id='', lease_until=0,"
                " last_error=?, updated_at=? WHERE status IN ('claimed','running')",
                (reason[:200] or "运维释放：跑分重启", now),
            )
            return cur.rowcount

    def is_completed(self, unique_code: str) -> bool:
        """是否已 solved（跨进程预检：避免重复解题）。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT status FROM challenges WHERE unique_code=?", (unique_code,)
            ).fetchone()
            return bool(row and row["status"] == "solved")

    def status(self, unique_code: str) -> str:
        with self._conn() as c:
            row = c.execute(
                "SELECT status FROM challenges WHERE unique_code=?", (unique_code,)
            ).fetchone()
            return str(row["status"]) if row else "unknown"

    def summary(self) -> dict:
        """账本汇总：各状态计数（跑分断点可视化）。"""
        with self._conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM challenges GROUP BY status").fetchall()
            return {r["status"]: r["n"] for r in rows}

    # ---------------- 状态同步（编排层"黑板读出通道"） ----------------

    def holders(self) -> dict[str, dict]:
        """当前占用快照：{unique_code: {worker, status, lease_left_s, owned}}。

        供 api.py holders 子命令 / start-409 提示注入——让模型在决策点
        看到全系统的容器持有状态（而不是撞 409 后凭局部信息瞎猜）。
        owned 标记是否为本 worker 持有（close 所有权判定依据）。
        """
        now = time.time()
        with self._conn() as c:
            rows = c.execute(
                "SELECT unique_code, status, worker_id, lease_until FROM challenges"
                " WHERE status IN ('claimed','running') AND lease_until > ?"
                " ORDER BY unique_code",
                (now,),
            ).fetchall()
        return {
            r["unique_code"]: {
                "worker": r["worker_id"],
                "status": r["status"],
                "lease_left_s": round(float(r["lease_until"]) - now, 1),
                "owned": r["worker_id"] == self.worker_id,
            }
            for r in rows
        }

    def active_foreign_leases(self) -> list[str]:
        """其他 worker 持有的、未过期且持有者仍存活的租约（跑分双开检测用）。

        复评修复：两个 run 并发打同一任务共抢平台容器上限（实测 62 次 409 互相
        饿死）——启动闸门据此快速失败；死进程的残留租约（pid 已不存在）不计入，
        避免崩溃后干等 15min TTL。
        """
        now = time.time()
        with self._conn() as c:
            rows = c.execute(
                "SELECT unique_code, worker_id FROM challenges"
                " WHERE status IN ('claimed','running') AND lease_until > ? AND worker_id != ?",
                (now, self.worker_id),
            ).fetchall()
        return [str(r["unique_code"]) for r in rows if _lease_holder_alive(str(r["worker_id"]))]

    def owns(self, unique_code: str) -> bool:
        """本 worker 是否持有该题租约（close 所有权判定）。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT worker_id FROM challenges"
                " WHERE unique_code=? AND status IN ('claimed','running') AND lease_until > ?",
                (unique_code, time.time()),
            ).fetchone()
        return bool(row and row["worker_id"] == self.worker_id)

    # ---------------- 跨进程完成广播（scope 维度） ----------------

    def mark_scope_done(self, scope_key: str, *, done_by: str = "") -> None:
        """置位资产/题目的完成广播（幂等，跨进程可见）。"""
        if not scope_key:
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO scope_done (scope_key, done_by, done_at) VALUES (?,?,?)",
                (scope_key, done_by or self.worker_id, time.time()),
            )

    def is_scope_done(self, scope_key: str) -> bool:
        """该资产/题目是否已被（任意进程的）意图完成。"""
        if not scope_key:
            return False
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM scope_done WHERE scope_key=?", (scope_key,)).fetchone()
        return row is not None

    def dump_json(self, dest: Path | str) -> None:
        """账本快照落盘（跑分复盘 / 断点续跑数据源）。"""
        with self._conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM challenges").fetchall()]
        Path(dest).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
