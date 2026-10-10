"""EvidenceStore：内容寻址证据快照与 findings 绑定（Phase B3）。

blob 按 SHA-256 内容寻址存盘（天然去重），索引 SQLite 记录 blobs 元数据与
finding -> blob 绑定（角色：baseline/proof/verification/supporting）。
exchange 快照序列化为标准信封格式（secpt-exchange@1），导出时还原成
request.http / response.http 风格文件 + manifest.json。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # 避免运行期循环依赖
    from secpt.traffic.store import TrafficStore

VALID_ROLES = ("baseline", "proof", "verification", "supporting")

EXCHANGE_FORMAT = "secpt-exchange"
EXCHANGE_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    role TEXT CHECK(role IN ('baseline','proof','verification','supporting')),
    exchange_id INTEGER,
    note TEXT DEFAULT '',
    ts REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bindings_finding ON bindings(finding_id);
CREATE INDEX IF NOT EXISTS idx_bindings_blob ON bindings(blob_hash);
"""


def _b64(data: bytes | None) -> str:
    return base64.b64encode(data).decode("ascii") if data else ""


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii")) if data else b""


def _headers_to_lines(headers: Any) -> list[str]:
    """dict / JSON 字符串形式的 headers 转成 'K: V' 行列表。"""
    if isinstance(headers, str):
        try:
            headers = json.loads(headers or "{}")
        except ValueError:
            return []
    if not isinstance(headers, dict):
        return []
    return [f"{k}: {v}" for k, v in headers.items()]


def serialize_exchange(ex: dict[str, Any]) -> bytes:
    """把 traffic 层的一条交换 dict 序列化为标准信封格式（bytes）。"""
    envelope = {
        "format": EXCHANGE_FORMAT,
        "version": EXCHANGE_VERSION,
        "method": ex.get("method") or "",
        "url": ex.get("url") or "",
        "status": int(ex.get("status") or 0),
        "request": {
            "headers": ex.get("req_headers") or {},
            "body_b64": _b64(ex.get("req_body")),
        },
        "response": {
            "headers": ex.get("resp_headers") or {},
            "body_b64": _b64(ex.get("resp_body")),
        },
        "ts": float(ex.get("ts") or 0),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def try_parse_exchange(data: bytes) -> dict[str, Any] | None:
    """若 blob 是 secpt-exchange 信封则解析返回，否则 None。"""
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("format") != EXCHANGE_FORMAT:
        return None
    return envelope


def _http_file_lines(envelope: dict[str, Any], side: str) -> tuple[str, bytes]:
    """把信封的一边还原成 request.http / response.http 风格内容。"""
    part = envelope.get(side) or {}
    headers = _headers_to_lines(part.get("headers"))
    body = _unb64(part.get("body_b64") or "")
    if side == "request":
        url = urlsplit(str(envelope.get("url") or ""))
        target = url.path or "/"
        if url.query:
            target += f"?{url.query}"
        lines = [f"{envelope.get('method') or 'GET'} {target} HTTP/1.1", *headers]
    else:
        reason = "OK" if int(envelope.get("status") or 0) < 400 else "ERROR"
        lines = [f"HTTP/1.1 {envelope.get('status') or 0} {reason}", *headers]
    head = "\r\n".join(lines).encode("utf-8", "ignore") + b"\r\n\r\n"
    return head.decode("latin-1"), body


class EvidenceStore:
    """内容寻址证据库：blobs 存盘 + SQLite 索引 + finding 绑定。"""

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root) if root else Path(os.environ.get("SECPT_VAR", "var")) / "evidence"
        self.root = Path(base)
        self.blobs_dir = self.root / "blobs"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite"
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ── blob 层 ──────────────────────────────────────────────

    def _blob_path(self, blob_hash: str) -> Path:
        return self.blobs_dir / blob_hash[:2] / f"{blob_hash}.bin"

    def store_blob(self, data: bytes) -> str:
        """按内容寻址存 blob，返回 SHA-256；已存在则跳过写入。"""
        digest = hashlib.sha256(data).hexdigest()
        path = self._blob_path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self._conn.execute(
            "INSERT OR IGNORE INTO blobs (hash, size, ts) VALUES (?,?,?)",
            (digest, len(data), time.time()),
        )
        self._conn.commit()
        return digest

    def get_blob(self, blob_hash: str) -> bytes | None:
        """取 blob 内容；不存在返回 None。"""
        path = self._blob_path(blob_hash)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def has_blob(self, blob_hash: str) -> bool:
        return self._blob_path(blob_hash).exists()

    # ── 绑定层 ───────────────────────────────────────────────

    def attach(
        self,
        finding_id: str,
        blob_hash: str,
        role: str,
        exchange_id: int | None = None,
        note: str = "",
    ) -> int | None:
        """把 blob 绑定到 finding；返回 binding id。blob 不存在返回 None。"""
        if role not in VALID_ROLES:
            raise ValueError(f"非法证据角色: {role!r}（合法值: {VALID_ROLES}）")
        if not self.has_blob(blob_hash):
            print(f"[evidence] blob 不存在，绑定失败: {blob_hash}")
            return None
        cur = self._conn.execute(
            "INSERT INTO bindings (finding_id, blob_hash, role, exchange_id, note, ts)"
            " VALUES (?,?,?,?,?,?)",
            (finding_id, blob_hash, role, exchange_id, note, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_for(self, finding_id: str) -> list[dict[str, Any]]:
        """列出 finding 的全部绑定（join blob 元数据），按时间排序。"""
        rows = self._conn.execute(
            "SELECT b.id, b.finding_id, b.blob_hash, b.role, b.exchange_id,"
            " b.note, b.ts, s.size"
            " FROM bindings b LEFT JOIN blobs s ON s.hash = b.blob_hash"
            " WHERE b.finding_id = ? ORDER BY b.id",
            (finding_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 快照与导出 ───────────────────────────────────────────

    def snapshot_from_exchange(self, traffic_store: TrafficStore, exchange_id: int) -> str | None:
        """从 traffic 库取一条交换，序列化成标准信封 blob，返回 hash。"""
        try:
            ex = traffic_store.get(exchange_id)
        except Exception as exc:
            print(f"[evidence] 读取 exchange {exchange_id} 失败: {exc}")
            return None
        if ex is None:
            print(f"[evidence] exchange 不存在: {exchange_id}")
            return None
        return self.store_blob(serialize_exchange(ex))

    def export_finding(self, finding_id: str, out_dir: str | Path) -> Path:
        """导出 finding 的全部证据：request/response 风格文件 + manifest.json。"""
        dest = Path(out_dir) / finding_id
        dest.mkdir(parents=True, exist_ok=True)
        items: list[dict[str, Any]] = []
        for binding in self.list_for(finding_id):
            blob = self.get_blob(binding["blob_hash"]) or b""
            envelope = try_parse_exchange(blob)
            written: list[str] = []
            prefix = f"{binding['id']:04d}_{binding['role']}"
            if envelope is not None:
                for side, suffix in (("request", "request.http"), ("response", "response.http")):
                    head, body = _http_file_lines(envelope, side)
                    fpath = dest / f"{prefix}_{suffix}"
                    fpath.write_bytes(head.encode("latin-1") + body)
                    written.append(fpath.name)
            else:
                fpath = dest / f"{prefix}.bin"
                fpath.write_bytes(blob)
                written.append(fpath.name)
            items.append(
                {
                    "binding_id": binding["id"],
                    "blob_hash": binding["blob_hash"],
                    "role": binding["role"],
                    "exchange_id": binding["exchange_id"],
                    "note": binding["note"],
                    "size": len(blob),
                    "files": written,
                }
            )
        manifest = {
            "finding_id": finding_id,
            "exported_at": time.time(),
            "count": len(items),
            "items": items,
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return dest

    def export_job(self, job_id: str, out_dir: str | Path) -> Path | None:
        """导出 job 的全部流量证据（整库 exchanges + manifest.json）。"""
        from secpt.traffic.store import default_traffic_dir

        db_path = default_traffic_dir() / f"{job_id}.sqlite"
        if not db_path.exists():
            print(f"[evidence] 该 job 无流量库: {job_id}")
            return None
        dest = Path(out_dir) / job_id
        dest.mkdir(parents=True, exist_ok=True)
        # 整库全量拉取（exchanges 是单 job 的库，量级可控）
        items: list[dict[str, Any]] = []
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM exchanges ORDER BY id").fetchall()
        finally:
            conn.close()
        for row in rows:
            ex = dict(row)
            blob_hash = self.store_blob(serialize_exchange(ex))
            prefix = f"exchange_{ex['id']:04d}"
            envelope = try_parse_exchange(self.get_blob(blob_hash) or b"")
            written: list[str] = []
            if envelope is not None:
                for side, suffix in (("request", "request.http"), ("response", "response.http")):
                    head, body = _http_file_lines(envelope, side)
                    fpath = dest / f"{prefix}_{suffix}"
                    fpath.write_bytes(head.encode("latin-1") + body)
                    written.append(fpath.name)
            items.append(
                {
                    "exchange_id": ex["id"],
                    "method": ex["method"],
                    "url": ex["url"],
                    "status": ex["status"],
                    "ts": ex["ts"],
                    "blob_hash": blob_hash,
                    "files": written,
                }
            )
        manifest = {
            "job_id": job_id,
            "exported_at": time.time(),
            "count": len(items),
            "items": items,
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return dest
