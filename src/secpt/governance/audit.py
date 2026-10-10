"""G5：审计持久化。

把 structlog 事件追加到 JSONL 文件，进程外可审计。
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class AuditLogger:
    """JSONL 审计日志。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **kwargs: Any) -> None:
        """写入一条审计记录。"""
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **kwargs,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            logger.error("audit.write_error", path=str(self._path), error=str(exc))

    def read_all(self) -> list[dict]:
        """读取全部审计记录。"""
        if not self._path.exists():
            return []
        records: list[dict] = []
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    with contextlib.suppress(json.JSONDecodeError):
                        records.append(json.loads(line))
        return records


__all__ = ["AuditLogger"]
