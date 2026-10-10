"""TrafficStore：HTTP 交换的 SQLite 落库与查询（流量证据层）。

设计思想参考 ARTEX 的明文录制（内部授权场景）：request+response 原样落库，
内容寻址与 findings 绑定由 evidence 层负责。body 单条限 256KB，防止大文件
下载撑爆库。有 FTS5 时建全文索引（url/method/body），没有则退回普通索引。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

MAX_BODY_BYTES = 256 * 1024  # 每条 body 落库上限 256KB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    status INTEGER NOT NULL DEFAULT 0,
    req_headers TEXT NOT NULL DEFAULT '',
    req_body BLOB,
    resp_headers TEXT NOT NULL DEFAULT '',
    resp_body BLOB,
    ts REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_exchanges_url ON exchanges(url);
CREATE INDEX IF NOT EXISTS idx_exchanges_method ON exchanges(method);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS exchanges_fts
USING fts5(url, method, req_body, resp_body);
"""


def default_traffic_dir() -> Path:
    """返回流量库根目录（<SECPT_VAR>/traffic）。"""
    return Path(os.environ.get("SECPT_VAR", "var")) / "traffic"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）一个流量 SQLite，并建好 schema。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def has_fts5(conn: sqlite3.Connection) -> bool:
    """探测当前 SQLite 是否支持 FTS5。"""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except sqlite3.Error:
        return False


def init_schema(conn: sqlite3.Connection) -> bool:
    """建表；返回是否成功创建 FTS5 索引。任何失败都 best-effort 降级。"""
    conn.executescript(_SCHEMA)
    if has_fts5(conn):
        try:
            conn.execute(_FTS_SCHEMA)
        except sqlite3.Error:
            return False
        return True
    return False


def _truncate_body(body: Any) -> bytes | None:
    """body 统一收敛为 bytes 并截断到 256KB；空/异常返回 None。"""
    if body is None:
        return None
    try:
        if isinstance(body, str):
            data = body.encode("utf-8", "ignore")
        elif isinstance(body, (bytes, bytearray, memoryview)):
            data = bytes(body)
        else:
            data = str(body).encode("utf-8", "ignore")
    except Exception:
        return None
    return data[:MAX_BODY_BYTES] or None


def _headers_to_json(headers: Any) -> str:
    """headers（dict / mitmproxy 风格对象）序列化成 JSON 字符串，best-effort。"""
    try:
        if headers is None:
            return "{}"
        if hasattr(headers, "items"):
            return json.dumps(dict(headers.items()), ensure_ascii=False)
        return json.dumps(dict(headers), ensure_ascii=False)
    except Exception:
        return "{}"


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exchanges_fts'"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def record_exchange(conn: sqlite3.Connection, flow: dict[str, Any]) -> int | None:
    """把一条 HTTP 交换（request+response 的纯 dict）落库，返回行 id。

    纯函数、可单测：addon 层只负责把 mitmproxy flow 拍平成 dict，落库逻辑在这里。
    任何异常吞掉返回 None（录制层绝不让代理进程崩溃）。
    """
    try:
        req_body = _truncate_body(flow.get("req_body"))
        resp_body = _truncate_body(flow.get("resp_body"))
        cur = conn.execute(
            "INSERT INTO exchanges (method, url, status, req_headers, req_body,"
            " resp_headers, resp_body, ts) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(flow.get("method") or ""),
                str(flow.get("url") or ""),
                int(flow.get("status") or 0),
                _headers_to_json(flow.get("req_headers")),
                req_body,
                _headers_to_json(flow.get("resp_headers")),
                resp_body,
                float(flow.get("ts") or time.time()),
            ),
        )
        row_id = int(cur.lastrowid)
        conn.commit()
        # FTS5 索引（best-effort：二进制 body 解码失败就跳过索引，主行已落）
        if _fts_available(conn):
            try:
                conn.execute(
                    "INSERT INTO exchanges_fts (rowid, url, method, req_body, resp_body)"
                    " VALUES (?,?,?,?,?)",
                    (
                        row_id,
                        str(flow.get("url") or ""),
                        str(flow.get("method") or ""),
                        req_body.decode("utf-8", "ignore") if req_body else "",
                        resp_body.decode("utf-8", "ignore") if resp_body else "",
                    ),
                )
                conn.commit()
            except sqlite3.Error:
                pass
        return row_id
    except Exception:
        return None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("req_headers", "resp_headers"):
        try:
            d[key] = json.loads(d.get(key) or "{}")
        except (TypeError, ValueError):
            d[key] = {}
    return d


def _escape_fts(query: str) -> str:
    """把自由文本转成 FTS5 安全查询：逐 token 加引号。"""
    return " ".join(f'"{tok}"' for tok in query.split() if tok)


class TrafficStore:
    """单个 job 的流量库：exchanges 落库 + search/get/stats 查询。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect(self.db_path)
        self._fts = _fts_available(self._conn)

    @property
    def fts_enabled(self) -> bool:
        return self._fts

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def record(self, flow: dict[str, Any]) -> int | None:
        """落一条交换（flow 为拍平后的纯 dict）。"""
        return record_exchange(self._conn, flow)

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """按关键词查 exchanges：FTS5 走全文匹配，否则 LIKE url/method/body。"""
        query = (query or "").strip()
        if not query:
            return []
        try:
            if self._fts:
                fts_query = _escape_fts(query)
                if fts_query:
                    rows = self._conn.execute(
                        "SELECT e.* FROM exchanges e"
                        " JOIN exchanges_fts f ON f.rowid = e.id"
                        " WHERE exchanges_fts MATCH ? ORDER BY e.id LIMIT ?",
                        (fts_query, int(limit)),
                    ).fetchall()
                    return [_row_to_dict(r) for r in rows]
            like = f"%{query}%"
            rows = self._conn.execute(
                "SELECT * FROM exchanges WHERE url LIKE ? OR method LIKE ?"
                " OR CAST(req_body AS TEXT) LIKE ? OR CAST(resp_body AS TEXT) LIKE ?"
                " ORDER BY id LIMIT ?",
                (like, like, like, like, int(limit)),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        except sqlite3.Error:
            return []

    def get(self, exchange_id: int) -> dict[str, Any] | None:
        """取一条交换；不存在返回 None。"""
        try:
            row = self._conn.execute(
                "SELECT * FROM exchanges WHERE id = ?", (int(exchange_id),)
            ).fetchone()
            return _row_to_dict(row) if row else None
        except sqlite3.Error:
            return None

    def stats(self) -> dict[str, Any]:
        """库内统计：总数、方法分布、状态码分布。"""
        try:
            total = self._conn.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0]
            methods = {
                str(r[0]): int(r[1])
                for r in self._conn.execute(
                    "SELECT method, COUNT(*) FROM exchanges GROUP BY method"
                )
            }
            statuses = {
                str(r[0]): int(r[1])
                for r in self._conn.execute(
                    "SELECT status, COUNT(*) FROM exchanges GROUP BY status"
                )
            }
            return {"exchanges": int(total), "methods": methods, "statuses": statuses}
        except sqlite3.Error:
            return {"exchanges": 0, "methods": {}, "statuses": {}}
