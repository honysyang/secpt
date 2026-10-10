"""G2：速率限制。

防止打挂目标、触发对方 IPS。

两层：
- RateLimiter        单进程内令牌桶（原有）
- GlobalRateLimiter  跨进程全局限速（S2）：按 target_host 在 SQLite 里计数，
                     多 job 共享配额。manager 进程内建单例，worker 的 bash
                     网络请求通过 hook 检查。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# 从 bash 命令里提取网络目标（URL / IP / host）
_HOST_RE = re.compile(
    r"(?:https?://|ftp://)?([a-zA-Z0-9.-]+(?:\.[a-zA-Z]{2,})|"
    r"\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?",
)
# 常见文件扩展名，排除（x.py / readme.md 不是 host）
_FILE_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".sh",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
    ".js",
    ".css",
    ".xml",
    ".log",
    ".csv",
    ".conf",
    ".cfg",
    ".c",
    ".h",
    ".go",
    ".rs",
    ".toml",
    ".ini",
    ".env",
}


class RateLimiter:
    """令牌桶限速器。"""

    def __init__(self, max_requests: int = 10, per_seconds: float = 1.0) -> None:
        self._max_requests = max_requests
        self._per_seconds = per_seconds
        self._tokens = float(max_requests)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """获取一个执行令牌，不足则等待。"""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # 补充令牌
            self._tokens = min(
                float(self._max_requests),
                self._tokens + elapsed * (self._max_requests / self._per_seconds),
            )
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * (self._per_seconds / self._max_requests)
                logger.debug("rate_limit.wait", wait=wait)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


def extract_hosts(command: str) -> list[str]:
    """从 bash 命令里提取网络目标 host 列表（去重、排除本地地址/文件扩展名）。"""
    hosts: list[str] = []
    for m in _HOST_RE.finditer(command):
        host = m.group(1)
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            continue
        lower = host.lower()
        if any(lower.endswith(sfx) for sfx in _FILE_SUFFIXES):
            continue
        if host not in hosts:
            hosts.append(host)
    return hosts


class GlobalRateLimiter:
    """跨进程全局限速：按 target_host 在 SQLite 里计数（WAL 原子递增）。

    使用方式（manager 内单例）：
        limiter = GlobalRateLimiter(db_path, max_rps=10.0)
        blocked = limiter.check(command)   # 返回被拦截的 host 列表，空 = 放行
    """

    def __init__(
        self, db_path: str | Path = "var/secpt.sqlite", max_rps: float = 10.0, window_s: float = 1.0
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_rps = max(max_rps, 0.1)
        self.window_s = max(window_s, 0.1)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ratelimit (
                    host         TEXT PRIMARY KEY,
                    window_start REAL NOT NULL,
                    count        INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def check(self, command: str) -> list[str]:
        """检查命令涉及的每个 host，超限则计数并返回被拦截 host；未超限计数并返回空。"""
        hosts = extract_hosts(command)
        if not hosts:
            return []
        blocked: list[str] = []
        now = time.monotonic()
        with self._conn() as conn:
            for host in hosts:
                row = conn.execute(
                    "SELECT window_start, count FROM ratelimit WHERE host=?", (host,)
                ).fetchone()
                if row is None or now - row[0] > self.window_s:
                    # 新窗口
                    conn.execute(
                        "INSERT INTO ratelimit (host, window_start, count) VALUES (?,?,?)"
                        " ON CONFLICT(host) DO UPDATE SET window_start=excluded.window_start, count=excluded.count",
                        (host, now, 1),
                    )
                else:
                    nxt = row[1] + 1
                    if nxt > self.max_rps:
                        blocked.append(host)
                    conn.execute(
                        "UPDATE ratelimit SET count=? WHERE host=?",
                        (min(nxt, self.max_rps), host),
                    )
        return blocked


__all__ = ["GlobalRateLimiter", "RateLimiter", "extract_hosts"]
