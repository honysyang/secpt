"""报告交付：findings → 专家可用的渗透报告（md / md-zip / json）。

黑客逻辑：洞的价值 = 严重度 × 可信度 × 可消费性。报告是放大器。
两个不变量：
- 只报 confirmed/inferred 分级的可信度（状态即承诺）
- 报告-证据一致性：记录报告生成时的 evidence_version，证据变了
  旧报告自动标 stale——杜绝拿旧证据交报告
"""

from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from pathlib import Path

from jinja2 import Template

from secpt.findings.models import SEVERITIES, Finding
from secpt.findings.store import FindingStore

_SEV_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

_MD_TEMPLATE = Template("""# 渗透测试报告 · {{ title }}

- 生成时间：{{ generated_at }}
- 任务：{{ job_id }}
- 目标：{{ target }}
- 发现统计：{% for s, n in stats.items() %}{{ s }}={{ n }} {% endfor %}

## 发现汇总

| 严重度 | 状态 | CWE | 标题 | 目标 |
|---|---|---|---|---|
{% for f in findings -%}
| {{ f.severity }} | {{ f.status }} | {{ f.cwe or '-' }} | {{ f.title }} | {{ f.target }} |
{% endfor %}

{% for f in findings %}
---

## [{{ f.severity | upper }}] {{ f.title }}

- **状态**：{{ f.status }}（confidence={{ "%.2f" | format(f.confidence) }}）
- **CWE**：{{ f.cwe or '待分类' }}
- **目标**：{{ f.target }}
- **首次发现**：job {{ f.first_seen.get('job_id', '-') }} step {{ f.first_seen.get('step', '-') }}

### 描述

{{ f.description or '（无）' }}

### 影响

{{ f.impact or '（无）' }}

### 复现步骤

```
{{ f.reproduction or '（无）' }}
```

### 证据

{% for e in f.evidence -%}
- [{{ e.role }}] {{ e.kind }}: {{ e.ref | truncate(120) }}
{% else -%}
- （无证据指针）
{% endfor %}
{% if f.reproduced_by %}
### 复现记录

{% for r in f.reproduced_by -%}
- {{ r.get('verdict') }} @ job {{ r.get('job_id') }}
{% endfor %}
{% endif %}
{% endfor %}
""")

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_versions (
    finding_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    reported_at REAL NOT NULL
);
"""


class ReportDelivery:
    """报告生成 + 证据一致性快照。"""

    def __init__(self, store: FindingStore | None = None) -> None:
        self.store = store or FindingStore()
        self.snap_path = self.store.db_path  # 同库，报告版本表

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.snap_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SNAPSHOT_SCHEMA)
        return conn

    # ---------------- 生成 ----------------

    def render_markdown(self, *, job_id: str | None = None, title: str | None = None) -> str:
        rows = self.store.list(limit=1000)
        if job_id:
            rows = [f for f in rows if f.job_id == job_id or f.first_seen.get("job_id") == job_id]
        rows.sort(key=lambda f: (_SEV_ORDER.get(f.severity, 9), f.status != "confirmed"))
        stats: dict[str, int] = {}
        for f in rows:
            stats[f.severity] = stats.get(f.severity, 0) + 1
        target = rows[0].target if rows else ""
        return _MD_TEMPLATE.render(
            title=title or (f"任务 {job_id}" if job_id else "全部发现汇总"),
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            job_id=job_id or "（跨任务）",
            target=target,
            stats=stats,
            findings=rows,
        )

    def render_json(self, *, job_id: str | None = None) -> str:
        rows = self.store.list(limit=1000)
        if job_id:
            rows = [f for f in rows if f.job_id == job_id or f.first_seen.get("job_id") == job_id]
        return json.dumps([f.model_dump() for f in rows], ensure_ascii=False, indent=2)

    def export_md_zip(self, out_path: Path, *, job_id: str | None = None) -> Path:
        """md-zip 证据包：report.md + 每 finding 的证据文件 + manifest.json。"""
        findings = self.store.list(limit=1000)
        if job_id:
            findings = [
                f for f in findings if f.job_id == job_id or f.first_seen.get("job_id") == job_id
            ]
        manifest: dict = {"generated_at": time.time(), "job_id": job_id, "findings": []}
        out_path = Path(out_path)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report.md", self.render_markdown(job_id=job_id))
            for f in findings:
                fdir = f"findings/{f.id}"
                for i, e in enumerate(f.evidence):
                    name = f"{fdir}/evidence-{i}-{e.role}.{'txt'}"
                    zf.writestr(name, e.ref)
                    manifest["findings"].append(
                        {
                            "id": f.id,
                            "title": f.title,
                            "status": f.status,
                            "severity": f.severity,
                            "cwe": f.cwe,
                            "evidence_files": name,
                        }
                    )
                zf.writestr(
                    f"{fdir}/finding.json", json.dumps(f.model_dump(), ensure_ascii=False, indent=2)
                )
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return out_path

    # ---------------- 证据一致性 ----------------

    def record_snapshot(self, finding_ids: list[str]) -> None:
        """报告生成时记录证据版本。"""
        with self._conn() as c:
            for fid in finding_ids:
                f = self.store.get(fid)
                if f is None:
                    continue
                c.execute(
                    """INSERT INTO report_versions (finding_id, version, reported_at)
                       VALUES (?,?,?)
                       ON CONFLICT(finding_id) DO UPDATE SET
                           version=excluded.version, reported_at=excluded.reported_at""",
                    (fid, f.evidence_version, time.time()),
                )

    def stale_findings(self) -> list[Finding]:
        """证据已变更、报告过期的 findings（一致性不变量）。"""
        out: list[Finding] = []
        with self._conn() as c:
            rows = c.execute("SELECT finding_id, version FROM report_versions").fetchall()
        for row in rows:
            f = self.store.get(row["finding_id"])
            if f is not None and f.evidence_version != row["version"]:
                out.append(f)
        return out

    # ---------------- 主入口 ----------------

    def generate(
        self, *, job_id: str | None = None, fmt: str = "md", out: Path | None = None
    ) -> Path:
        out = Path(out or Path("report") / f"report-{job_id or 'all'}.{fmt}")
        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            out.write_text(self.render_json(job_id=job_id), encoding="utf-8")
        elif fmt == "md-zip":
            self.export_md_zip(out, job_id=job_id)
        else:
            out.write_text(self.render_markdown(job_id=job_id), encoding="utf-8")
        # 记录快照（一致性）
        rows = self.store.list(limit=1000)
        if job_id:
            rows = [f for f in rows if f.job_id == job_id or f.first_seen.get("job_id") == job_id]
        self.record_snapshot([f.id for f in rows])
        return out
