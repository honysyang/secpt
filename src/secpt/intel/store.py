"""IntelStore：双图存储（SQLite）。

设计红线（对齐 ARTEX）：
- dedup_key 由程序计算（type + 归一化 value），agent 永远提交原始观察
- 边是有向血缘（spawns/derived_from/yields/proves），穷举交叉的原材料
- FTS5 可用时建事实全文索引（recall 工具的底座），不可用时优雅降级 LIKE
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite"

ASSET_TYPES = ("root_domain", "subdomain", "ip", "service", "app", "endpoint")
NODE_TYPES = ("goal", "intent", "fact", "finding", "hint")
EDGE_KINDS = ("spawns", "derived_from", "yields", "proves")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    parent_id INTEGER,
    meta TEXT DEFAULT '{}',
    first_seen_job TEXT DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(type);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    job_id TEXT DEFAULT '',
    step INTEGER DEFAULT 0,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

CREATE TABLE IF NOT EXISTS anchors (
    node_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    UNIQUE(node_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_anchors_asset ON anchors(asset_id);
"""

_URL_RE = re.compile(r"https?://([^/?\s]+)([^?\s]*)")


def normalize_asset(atype: str, value: str) -> str:
    """程序侧归一化 + dedup key（去重逻辑绝不交给 LLM）。协议/大小写/尾斜杠不敏感。"""
    v = value.strip().lower().rstrip("/")
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)  # 剥协议（example.com == http://example.com/）
    if atype == "endpoint":
        # v 已剥协议：host/path?query → key 只留 host+path
        m = re.match(r"^([^/?\s]+)([^?\s]*)", v)
        if m:
            return f"endpoint:{m.group(1)}{m.group(2)}"
        return f"endpoint:{v}"
    if atype in ("root_domain", "subdomain"):
        return f"{atype}:{v.lstrip('*').lstrip('.')}"
    if atype == "ip":
        return f"ip:{v}"
    if atype == "service":
        # host:port 归一
        return f"service:{v.replace('://', ':')}"
    if atype == "app":
        return f"app:{v}"
    return f"{atype}:{v}"


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts_t USING fts5(x)")
        conn.execute("DROP TABLE temp._fts_t")
        return True
    except sqlite3.Error:
        return False


class IntelStore:
    """双图存储。全部方法幂等/容错（记忆层 best-effort，不阻断主链路）。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._fts = _fts5_available(c)
            if self._fts:
                c.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                       USING fts5(content, node_id UNINDEXED)"""
                )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------- 资产图 ----------------

    def observe_asset(
        self,
        atype: str,
        value: str,
        *,
        parent_id: int | None = None,
        meta: dict | None = None,
        job_id: str = "",
    ) -> int:
        """提交原始观察 → 程序算 dedup/父子，返回资产 id（已存在则复用）。"""
        if atype not in ASSET_TYPES:
            raise ValueError(f"资产类型必须是 {ASSET_TYPES}，收到 {atype!r}")
        key = normalize_asset(atype, value)
        with self._conn() as c:
            row = c.execute("SELECT id FROM assets WHERE dedup_key=?", (key,)).fetchone()
            if row:
                return int(row["id"])
            cur = c.execute(
                """INSERT INTO assets (type,value,dedup_key,parent_id,meta,first_seen_job,ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    atype,
                    value.strip()[:500],
                    key,
                    parent_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                    job_id,
                    time.time(),
                ),
            )
            return int(cur.lastrowid)

    def find_asset(self, atype: str, value: str) -> int | None:
        key = normalize_asset(atype, value)
        with self._conn() as c:
            row = c.execute("SELECT id FROM assets WHERE dedup_key=?", (key,)).fetchone()
        return int(row["id"]) if row else None

    def assets(self, *, atype: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM assets WHERE 1=1", []
        if atype:
            sql += " AND type=?"
            args.append(atype)
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # ---------------- 探索图 ----------------

    def add_node(
        self,
        ntype: str,
        content: str,
        *,
        job_id: str = "",
        step: int = 0,
        anchor_assets: list[int] | None = None,
    ) -> int:
        if ntype not in NODE_TYPES:
            raise ValueError(f"节点类型必须是 {NODE_TYPES}，收到 {ntype!r}")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO nodes (type,content,job_id,step,ts) VALUES (?,?,?,?,?)",
                (ntype, content[:2000], job_id, step, time.time()),
            )
            nid = int(cur.lastrowid)
            if self._fts and ntype in ("fact", "finding", "hint"):
                c.execute(
                    "INSERT INTO facts_fts (content, node_id) VALUES (?,?)", (content[:2000], nid)
                )
            for aid in anchor_assets or []:
                c.execute(
                    "INSERT OR IGNORE INTO anchors (node_id,asset_id,ts) VALUES (?,?,?)",
                    (nid, aid, time.time()),
                )
        return nid

    def add_edge(self, src: int, dst: int, kind: str) -> int:
        if kind not in EDGE_KINDS:
            raise ValueError(f"边类型必须是 {EDGE_KINDS}，收到 {kind!r}")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO edges (src,dst,kind,ts) VALUES (?,?,?,?)",
                (src, dst, kind, time.time()),
            )
            return int(cur.lastrowid)

    def recall(self, query: str, *, limit: int = 8) -> list[dict]:
        """全文/模糊检索 fact/finding/hint（agent 的过程级记忆检索）。

        分词策略：纯 ASCII 词走 FTS5 MATCH；含中文则按词项 LIKE-AND——
        FTS5 默认分词器对连续 CJK 不切词，中文检索必须逐词项 AND 才精确。
        """
        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return []
        out: list[dict] = []
        with self._conn() as c:
            has_cjk = any(re.search(r"[\u4e00-\u9fff]", t) for t in terms)
            if self._fts and not has_cjk:
                try:
                    match = " AND ".join(f'"{t}"' for t in terms[:8])
                    rows = c.execute(
                        """SELECT n.* FROM facts_fts f JOIN nodes n ON n.id=f.node_id
                           WHERE facts_fts MATCH ? ORDER BY n.ts DESC LIMIT ?""",
                        (match, limit),
                    ).fetchall()
                    out = [dict(r) for r in rows]
                except sqlite3.Error:
                    out = []
            if not out:
                sql = """SELECT * FROM nodes WHERE type IN ('fact','finding','hint')
                         AND (1=1"""
                args: list = []
                for t in terms[:6]:
                    sql += " AND content LIKE ?"
                    args.append(f"%{t[:40]}%")
                sql += ") ORDER BY ts DESC LIMIT ?"
                args.append(limit)
                rows = c.execute(sql, args).fetchall()
                out = [dict(r) for r in rows]
        return out

    def lineage(self, node_id: int) -> dict:
        """一个节点的血缘子图（出边+入边一层）。"""
        with self._conn() as c:
            node = c.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            out_e = c.execute(
                """SELECT e.kind, e.dst, n.type AS dst_type, n.content AS dst_content
                   FROM edges e JOIN nodes n ON n.id=e.dst WHERE e.src=?""",
                (node_id,),
            ).fetchall()
            in_e = c.execute(
                """SELECT e.kind, e.src, n.type AS src_type, n.content AS src_content
                   FROM edges e JOIN nodes n ON n.id=e.src WHERE e.dst=?""",
                (node_id,),
            ).fetchall()
        return {
            "node": dict(node) if node else None,
            "yields": [dict(r) for r in out_e],
            "derived_from": [dict(r) for r in in_e],
        }

    # ---------------- 覆盖度 ----------------

    def coverage(self, *, atype: str | None = None) -> dict:
        """资产覆盖度：有锚点（被任何探索节点引用）= 已触达。"""
        sql = """
            SELECT a.type, COUNT(*) AS total,
                   SUM(CASE WHEN an.asset_id IS NOT NULL THEN 1 ELSE 0 END) AS touched
            FROM assets a
            LEFT JOIN (SELECT DISTINCT asset_id FROM anchors) an ON an.asset_id = a.id
        """
        args: list = []
        if atype:
            sql += " WHERE a.type=?"
            args.append(atype)
        sql += " GROUP BY a.type"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return {r["type"]: {"total": r["total"], "touched": r["touched"] or 0} for r in rows}

    def untested_assets(self, *, atype: str = "endpoint", limit: int = 50) -> list[dict]:
        """未触达资产清单（ planner 的待办原材料 ）。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT a.* FROM assets a
                   WHERE a.type=? AND a.id NOT IN (SELECT DISTINCT asset_id FROM anchors)
                   ORDER BY a.id LIMIT ?""",
                (atype, limit),
            ).fetchall()
        return [dict(r) for r in rows]
