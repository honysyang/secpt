"""FindingStore：跨 job 存活的漏洞库（SQLite）。

findings 独立于任务生命周期——任务删了洞还在（真实 engagement 的洞要跨任务积累）。
9 态分诊的状态层（resolved/fixed 等交付态在 C1 扩展 STATUSES）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import ClassVar

from secpt.findings.models import STATUSES, Finding

DEFAULT_DB = Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    vuln_class TEXT DEFAULT '',
    severity TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'inferred',
    confidence REAL DEFAULT 0.5,
    target TEXT DEFAULT '',
    description TEXT DEFAULT '',
    reproduction TEXT DEFAULT '',
    impact TEXT DEFAULT '',
    evidence TEXT DEFAULT '[]',
    cwe TEXT DEFAULT '',
    cve_candidates TEXT DEFAULT '[]',
    first_seen TEXT DEFAULT '{}',
    reproduced_by TEXT DEFAULT '[]',
    job_id TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0,
    evidence_version INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_target ON findings(target);
"""


class FindingStore:
    """漏洞库。状态迁移有白名单（确认纪律不可绕过）。"""

    # 合法状态迁移（confirmation discipline：inferred 只能经 verify 升级 confirmed）
    # 9 态分诊（对齐真实 engagement 分诊生命周期）：
    # resolved/risk_accepted=交付态，false_positive/duplicate/ignored=排除态
    TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "inferred": {"in_progress", "confirmed", "false_positive", "duplicate", "ignored"},
        "in_progress": {"confirmed", "false_positive", "duplicate", "ignored"},
        "confirmed": {"resolved", "risk_accepted", "false_positive", "duplicate", "ignored"},
        "resolved": {"confirmed"},  # 复测 fixed 后可人工回归
        "risk_accepted": {"confirmed"},
        "false_positive": {"inferred"},  # 误判可翻案（重新验证）
        "duplicate": set(),
        "ignored": {"inferred"},
    }

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------- CRUD ----------------

    def save(self, f: Finding) -> Finding:
        now = time.time()
        f.created_at = f.created_at or now
        f.updated_at = now
        with self._conn() as c:
            c.execute(
                """INSERT INTO findings (id,title,vuln_class,severity,status,confidence,
                       target,description,reproduction,impact,evidence,cwe,cve_candidates,
                       first_seen,reproduced_by,job_id,created_at,updated_at,evidence_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       title=excluded.title, vuln_class=excluded.vuln_class,
                       severity=excluded.severity, status=excluded.status,
                       confidence=excluded.confidence, target=excluded.target,
                       description=excluded.description, reproduction=excluded.reproduction,
                       impact=excluded.impact, evidence=excluded.evidence, cwe=excluded.cwe,
                       cve_candidates=excluded.cve_candidates, first_seen=excluded.first_seen,
                       reproduced_by=excluded.reproduced_by, job_id=excluded.job_id,
                       updated_at=excluded.updated_at, evidence_version=excluded.evidence_version""",
                (
                    f.id,
                    f.title,
                    f.vuln_class,
                    f.severity,
                    f.status,
                    f.confidence,
                    f.target,
                    f.description,
                    f.reproduction,
                    f.impact,
                    json.dumps([e.model_dump() for e in f.evidence], ensure_ascii=False),
                    f.cwe,
                    json.dumps(f.cve_candidates, ensure_ascii=False),
                    json.dumps(f.first_seen, ensure_ascii=False),
                    json.dumps(f.reproduced_by, ensure_ascii=False),
                    f.job_id,
                    f.created_at,
                    f.updated_at,
                    f.evidence_version,
                ),
            )
        return f

    def get(self, finding_id: str) -> Finding | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        return self._row_to_finding(row) if row else None

    def list(
        self, *, status: str | None = None, target: str | None = None, limit: int = 100
    ) -> list[Finding]:
        sql, args = "SELECT * FROM findings WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if target:
            sql += " AND target LIKE ?"
            args.append(f"%{target}%")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._row_to_finding(r) for r in rows]

    # ---------------- 状态机 ----------------

    def transition(self, finding_id: str, new_status: str, *, note: str = "") -> Finding:
        """白名单状态迁移；inferred→confirmed 必须经 verify（本方法拒绝直接调）。

        :raises ValueError: 非法迁移
        """
        f = self.get(finding_id)
        if f is None:
            raise KeyError(finding_id)
        if new_status not in STATUSES:
            raise ValueError(f"未知状态: {new_status}")
        allowed = self.TRANSITIONS.get(f.status, set())
        if f.status == "inferred" and new_status == "confirmed":
            raise ValueError(
                "inferred→confirmed 必须经对抗式复现（secpt verify <id>），"
                "不允许手动盖章——证伪和证实同等有价值"
            )
        if new_status not in allowed:
            raise ValueError(f"非法迁移: {f.status} → {new_status}")
        f.status = new_status  # type: ignore[assignment]
        f.touch()
        return self.save(f)

    def record_reproduction(self, finding_id: str, *, job_id: str, verdict: str) -> Finding:
        """记录一次复现尝试（verify/retest 的落点）。"""
        f = self.get(finding_id)
        if f is None:
            raise KeyError(finding_id)
        f.reproduced_by.append({"job_id": job_id, "verdict": verdict, "ts": time.time()})
        if verdict == "reproduced" and f.status == "inferred":
            f.status = "confirmed"  # 对抗式复现成功 → 升级（唯一合法路径）
        f.touch()
        return self.save(f)

    # ---------------- 内部 ----------------

    def _row_to_finding(self, row: sqlite3.Row) -> Finding:
        d = dict(row)
        d["evidence"] = json.loads(d.get("evidence") or "[]")
        d["cve_candidates"] = json.loads(d.get("cve_candidates") or "[]")
        d["first_seen"] = json.loads(d.get("first_seen") or "{}")
        d["reproduced_by"] = json.loads(d.get("reproduced_by") or "[]")
        return Finding.model_validate(d)
