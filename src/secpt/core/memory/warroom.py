"""warroom：SQLite 四表共享作战记忆（历史/可选组件）。

多角色（recon / exploit / analyze / verify）协作的共享事实层：
角色之间**只交换这四张表的行**，不交换自然语言摘要。

    assets      —— 资产：host / port / service / tech，来源步骤 source_step
    credentials —— 凭据：kind / value，valid 是否已验证（S6 由 verify 置位）
    findings    —— 发现：id / kind / title / evidence / severity / verified
    attempts    —— 尝试记录：key_hash = sha1(action + target)，去重靠它

现状标注（F4）：当前多意图协作的生产链路走 intel 图 + findings 库 +
search_traces + 完成广播（见 core/orchestrator/frontier.py），本模块**无生产
装配点**——WarroomHooks（挂上 hooks 链即自动把观察点落四表）保留作通用桥接，
需要"表行级"协作语义的场景可显式接入；勿在新代码里默认依赖它。

并发要点（与 S1 JobStore 一致）：
- PRAGMA journal_mode=WAL + busy_timeout=5000，同一 job 内多链并发读写安全
- 每个操作独立开连接、短事务（sqlite3 连接不跨线程复用）

attempts 去重语义：同一 action+target 只记第一条，重复命中直接跳过。
这是防"并行链互相踩同一尝试"的关键。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# PR-D：busy 重试参数（WAL 下写写冲突仍可能 OperationalError: database is locked）
_BUSY_RETRY = 3  # 首次失败后最多重试 3 次
_BUSY_BASE_S = 0.1  # 指数退避：0.1s / 0.2s / 0.4s


def _is_busy(e: sqlite3.OperationalError) -> bool:
    msg = str(e).lower()
    return "lock" in msg or "busy" in msg


def attempt_key(action: str, target: str) -> str:
    """尝试去重键：sha1(action + target) 十六进制。"""
    return hashlib.sha1(f"{action}|{target}".encode()).hexdigest()


class Warroom:
    """SQLite L2 共享作战记忆。db_path 默认 var/jobs/<job_id>/warroom.sqlite。"""

    def __init__(self, db_path: str | Path, job_id: str = "") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.job_id = job_id
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

    def _write(self, fn):
        """短事务写 + busy 重试（指数退避）：写写冲突时退避后重开连接重试，
        避免 'database is locked' 导致写入被吞或上抛。非 busy 异常直接上抛。"""
        last: Exception | None = None
        for attempt in range(_BUSY_RETRY + 1):
            try:
                with self._conn() as conn:
                    return fn(conn)
            except sqlite3.OperationalError as e:
                if not _is_busy(e) or attempt == _BUSY_RETRY:
                    raise
                last = e
                time.sleep(_BUSY_BASE_S * (2**attempt))
        raise last  # pragma: no cover - 循环必在最后一轮 return 或 raise

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      TEXT NOT NULL DEFAULT '',
                    host        TEXT NOT NULL,
                    port        INTEGER,
                    service     TEXT NOT NULL DEFAULT '',
                    tech        TEXT NOT NULL DEFAULT '',
                    source_step TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_assets_host ON assets(host);
                CREATE TABLE IF NOT EXISTS credentials (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      TEXT NOT NULL DEFAULT '',
                    kind        TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    source_step TEXT NOT NULL DEFAULT '',
                    valid       INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id        TEXT NOT NULL,
                    job_id    TEXT NOT NULL DEFAULT '',
                    kind      TEXT NOT NULL,
                    title     TEXT NOT NULL,
                    evidence  TEXT NOT NULL DEFAULT '',
                    severity  TEXT NOT NULL DEFAULT 'info',
                    verified  INTEGER NOT NULL DEFAULT 0,
                    ts        REAL NOT NULL,
                    PRIMARY KEY (job_id, id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id    TEXT NOT NULL DEFAULT '',
                    key_hash  TEXT NOT NULL,
                    action    TEXT NOT NULL,
                    result    TEXT NOT NULL DEFAULT '',
                    reason    TEXT NOT NULL DEFAULT '',
                    UNIQUE (job_id, key_hash)
                );
                """
            )

    # ---------------- assets ----------------

    def add_asset(
        self,
        host: str,
        port: int | None = None,
        service: str = "",
        tech: str = "",
        source_step: str = "",
    ) -> int:
        """写入一条资产，返回 id。"""
        return (
            self._write(
                lambda conn: (
                    conn.execute(
                        "INSERT INTO assets (job_id, host, port, service, tech, source_step)"
                        " VALUES (?,?,?,?,?,?)",
                        (self.job_id, host, port, service, tech, source_step),
                    ).lastrowid
                )
            )
            or 0
        )

    def list_assets(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, host, port, service, tech, source_step FROM assets ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def count_assets(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()
        return int(row["n"])

    # ---------------- credentials ----------------

    def add_credential(
        self, kind: str, value: str, source_step: str = "", valid: bool = False
    ) -> int:
        return (
            self._write(
                lambda conn: (
                    conn.execute(
                        "INSERT INTO credentials (job_id, kind, value, source_step, valid)"
                        " VALUES (?,?,?,?,?)",
                        (self.job_id, kind, value, source_step, int(valid)),
                    ).lastrowid
                )
            )
            or 0
        )

    def list_credentials(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, kind, value, source_step, valid FROM credentials ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- findings ----------------

    def add_finding(
        self,
        fid: str,
        kind: str,
        title: str,
        evidence: str = "",
        severity: str = "info",
        verified: bool = False,
    ) -> None:
        self._write(
            lambda conn: conn.execute(
                """INSERT INTO findings (id, job_id, kind, title, evidence, severity, verified, ts)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id, id) DO UPDATE SET
                       kind=excluded.kind, title=excluded.title,
                       evidence=excluded.evidence, severity=excluded.severity,
                       verified=CASE WHEN excluded.verified THEN 1 ELSE verified END""",
                (
                    fid,
                    self.job_id,
                    kind,
                    title,
                    evidence,
                    severity,
                    int(verified),
                    __import__("time").time(),
                ),
            )
        )

    def list_findings(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, kind, title, evidence, severity, verified, ts FROM findings ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_verified(self, fid: str, verified: bool = True) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE findings SET verified=? WHERE job_id=? AND id=?",
                (int(verified), self.job_id, fid),
            )
        )

    def count_verified(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM findings WHERE verified=1").fetchone()
        return int(row["n"])

    # ---------------- attempts（去重） ----------------

    def add_attempt(
        self, action: str, target: str = "", result: str = "", reason: str = ""
    ) -> bool:
        """记录一次尝试。key = sha1(action + target)。

        返回 True 表示新增（之前未尝试过），False 表示重复命中被跳过。
        """
        key = attempt_key(action, target)
        rowcount = self._write(
            lambda conn: (
                conn.execute(
                    """INSERT OR IGNORE INTO attempts
                       (job_id, key_hash, action, result, reason)
                   VALUES (?,?,?,?,?)""",
                    (self.job_id, key, action, result[:1000], reason[:500]),
                ).rowcount
            )
        )
        return rowcount > 0

    def has_attempt(self, action: str, target: str = "") -> bool:
        key = attempt_key(action, target)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM attempts WHERE job_id=? AND key_hash=?",
                (self.job_id, key),
            ).fetchone()
        return row is not None

    def update_attempt_result(self, action: str, target: str = "", result: str = "") -> bool:
        """补记结果：按 key_hash 定位已有行 UPDATE result（PR-A 修正：原 INSERT OR IGNORE
        撞 UNIQUE 后 result 永远不落盘）。行不存在返回 False。"""
        key = attempt_key(action, target)
        rowcount = self._write(
            lambda conn: (
                conn.execute(
                    "UPDATE attempts SET result=? WHERE job_id=? AND key_hash=?",
                    (result[:1000], self.job_id, key),
                ).rowcount
            )
        )
        return rowcount > 0

    def list_attempts(self, limit: int = 500) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, key_hash, action, result, reason FROM attempts"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_attempts(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()
        return int(row["n"])


# ---------------------------------------------------------------------------
# 引擎观察点 → warroom 表行（原 core/orchestrator/specialist.py 迁入；该编排器
# 已被 frontier 角色意图折叠退役，WarroomHooks 作为 warroom 通用桥接保留——
# 挂到 hooks 链即把工具调用/发现自动落四张表）
# ---------------------------------------------------------------------------


def _parse_asset_url(url: str) -> tuple[str, int | None, str]:
    """从 URL 提取 (host, port, service)。解析失败返回空值。"""
    from urllib.parse import urlparse

    try:
        u = urlparse(url)
        return u.hostname or "", u.port, u.scheme or ""
    except Exception:
        return "", None, ""


def _extract_target(args: dict) -> str:
    """从工具参数里尽力提取 target（URL / 路径 / 命令里的地址），供 attempts 去重。"""
    if not isinstance(args, dict):
        return ""
    for key in ("url", "host", "target", "command"):
        v = str(args.get(key, "") or "")
        if not v:
            continue
        for tok in v.split():
            if tok.startswith("http://") or tok.startswith("https://"):
                return tok
            if tok.startswith(("127.", "10.", "192.168.", "172.")):
                return tok
        return v[:120]
    return ""


class WarroomHooks:
    """把 Engine 的观察点写进 warroom 四张表，并透传内层 hooks 链。

    链：Engine → WarroomHooks → Watchdog → EventSink
    - on_tool / on_result   → attempts（key=sha1(action+target)，重复跳过）
    - on_finding            → credentials（credential）/ assets（URL 解析）
    - 其余 hooks 经 __getattr__ 透传给内层（Watchdog）
    """

    def __init__(self, inner, warroom: Warroom) -> None:
        import json

        self._inner = inner
        self._warroom = warroom
        self._json = json
        # PR-A：按 call_id 精确关联（并行多链下 FIFO 顺序会被其他链交错打乱；
        # LLM 产出的 call_id 全局唯一，dict 映射天然并发安全）
        self._pending: dict[str, tuple[str, str]] = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # ---------------- attempts ----------------

    def on_tool(self, call_id: str, name: str, args: dict) -> None:
        action = f"{name}({self._json.dumps(args, ensure_ascii=False)[:200]})"
        target = _extract_target(args)
        self._pending[call_id] = (action, target)
        try:
            self._warroom.add_attempt(action, target=target)
        except Exception as e:
            print(f"[warroom] add_attempt failed (ignored): {e}")
        inner = getattr(self._inner, "on_tool", None)
        if inner is not None:
            inner(call_id, name, args)

    def on_result(self, call_id: str, out: str) -> None:
        pending = self._pending.pop(call_id, None)
        if pending is not None:
            action, target = pending
            # 补记结果：UPDATE 已有行（INSERT OR IGNORE 撞 UNIQUE 后 result 不落盘）
            try:
                self._warroom.update_attempt_result(action, target=target, result=out[:1000])
            except Exception as e:
                print(f"[warroom] update attempt failed (ignored): {e}")
        inner = getattr(self._inner, "on_result", None)
        if inner is not None:
            inner(call_id, out)

    # ---------------- findings ----------------

    def on_finding(self, kind: str, value: str, evidence: str = "") -> None:
        try:
            if kind == "credential":
                self._warroom.add_credential("regex", value, source_step=evidence)
                self._warroom.add_finding(
                    f"cred-{self._warroom.count_attempts() + 1}",
                    "credential",
                    value,
                    evidence=evidence,
                    severity="medium",
                )
            elif kind == "asset":
                host, port, service = _parse_asset_url(value)
                if host:
                    self._warroom.add_asset(host, port, service, "", source_step=evidence)
            elif kind == "flag":
                self._warroom.add_finding(
                    f"flag-{self._warroom.count_attempts() + 1}",
                    "flag",
                    value,
                    evidence=evidence,
                    severity="critical",
                )
        except Exception as e:
            print(f"[warroom] on_finding failed (ignored): {e}")
        inner = getattr(self._inner, "on_finding", None)
        if inner is not None:
            inner(kind, value, evidence)
        elif hasattr(self._inner, "finding"):
            self._inner.finding(kind, value, evidence)
