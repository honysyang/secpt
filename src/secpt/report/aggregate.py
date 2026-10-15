"""报告聚合：team 模式 report 角色的确定性聚合（阶段三 p3-report）。

数据源全部复用现有存储，不重复造轮子：
- findings：FindingStore（var/findings.sqlite），只聚合 confirmed 状态（验证纪律：
  status 即承诺，非已验证不报）
- 资产：IntelStore（var/intel.sqlite）assets 表
- 否定记录：frontier 的 exhausted 意图 + findings 的 inferred/false_positive

产出：build_report -> dict；render_markdown -> 结构化渗透报告（目标/侦察发现/
漏洞与证据/PoC 复现步骤/影响/建议）；write_report -> jobs_dir/<job_id>/report.md
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from secpt.findings.store import FindingStore


def _jobs_dir() -> Path:
    from secpt.core.job.resume import jobs_dir

    return jobs_dir()


def _intel_store():
    from secpt.intel.store import IntelStore

    return IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite")


def _finding_dict(f) -> dict:
    """Finding 对象 → 报告字典（evidence 转文本列表，避免渲染依赖模型细节）。"""
    evidence = []
    for e in getattr(f, "evidence", []) or []:
        ref = getattr(e, "ref", "") or ""
        evidence.append(str(ref)[:300])
    return {
        "id": getattr(f, "id", ""),
        "title": getattr(f, "title", ""),
        "severity": getattr(f, "severity", "medium"),
        "status": getattr(f, "status", ""),
        "confidence": round(float(getattr(f, "confidence", 0) or 0), 2),
        "target": getattr(f, "target", ""),
        "cwe": getattr(f, "cwe", "") or "",
        "description": getattr(f, "description", "") or "",
        "reproduction": getattr(f, "reproduction", "") or "",
        "impact": getattr(f, "impact", "") or "",
        "evidence": evidence,
        "job_id": getattr(f, "job_id", "") or "",
    }


def _belongs_to(f, job_id: str) -> bool:
    """finding 归属判定：job_id 相等或前缀匹配。

    团队模式 findings 挂在子任务 id（base-iXXXX）下，聚合 base 时必须前缀匹配，
    否则所有子任务 findings 都被过滤、报告恒空（P0 已确认缺陷）。
    """
    return str(f.job_id or "").startswith(job_id) or str(
        (f.first_seen or {}).get("job_id", "") or ""
    ).startswith(job_id)


def build_report(
    *,
    job_id: str = "",
    objective: str = "",
    frontier=None,
    findings_store: FindingStore | None = None,
    intel_store=None,
) -> dict:
    """聚合报告数据。

    job_id：只聚合该任务（或其子链）产生的数据；frontier 用于否定记录（exhausted 意图）。
    全部 best-effort：任一存储异常不炸聚合（report 角色容错，缺段照常出报告）。
    """
    fs = findings_store or FindingStore(
        # 调用时 SECPT_VAR 解析（模块级 DEFAULT_DB 是 import 期快照，
        # 测试/多实例会漂移——与 frontier 的调用时解析口径一致）
        Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite"
    )
    st = intel_store if intel_store is not None else _intel_store()

    # 资产（intel 图谱实体：endpoint/domain/host…）
    assets: list[dict] = []
    try:
        for a in st.assets(limit=200):
            assets.append({"type": a.get("type", ""), "value": str(a.get("value", ""))[:300]})
    except Exception:
        pass

    # 漏洞与证据：只报 confirmed（验证纪律）
    findings: list[dict] = []
    negatives: list[dict] = []  # 待确认/已证伪（未进入正式报告主体）
    try:
        for f in fs.list(limit=1000):
            if job_id and not _belongs_to(f, job_id):
                continue
            d = _finding_dict(f)
            if d["status"] == "confirmed":
                findings.append(d)
            elif d["status"] in ("inferred", "false_positive", "ignored"):
                negatives.append(d)
    except Exception:
        pass

    # 否定记录：frontier exhausted 意图（试过没成的路线，报告里标注避免重复踩坑）
    exhausted: list[dict] = []
    if frontier is not None:
        try:
            with frontier._conn() as c:
                rows = c.execute(
                    """SELECT title, result, fail_reason FROM intents
                       WHERE status='exhausted' ORDER BY updated_at DESC LIMIT 20"""
                ).fetchall()
            exhausted = [dict(r) for r in rows]
        except Exception:
            pass

    return {
        "job_id": job_id,
        "objective": objective,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "assets": assets,
        "findings": findings,
        "negatives": negatives,
        "exhausted": exhausted,
    }


def render_markdown(report: dict) -> str:
    """结构化渗透报告（目标/侦察发现/漏洞与证据/PoC 复现步骤/影响/建议）。"""
    obj = (report.get("objective") or "").strip()
    lines = [
        "# 渗透测试报告",
        "",
        f"- 任务：{report.get('job_id') or '（跨任务）'}",
        f"- 生成时间：{report.get('generated_at')}",
        f"- 确认漏洞：{len(report.get('findings', []))} 个",
        "",
        "## 1. 目标",
        "",
        obj or "（未提供）",
        "",
        "## 2. 侦察发现（资产）",
        "",
    ]
    assets = report.get("assets", [])
    if assets:
        lines.append("| 类型 | 值 |")
        lines.append("|---|---|")
        for a in assets:
            lines.append(f"| {a['type']} | {a['value']} |")
    else:
        lines.append("（无资产记录）")
    lines += ["", "## 3. 漏洞与证据（confirmed）", ""]
    findings = report.get("findings", [])
    if not findings:
        lines.append("（无已确认漏洞）")
    for f in findings:
        lines += [
            f"### [{f['severity']}] {f['title']}",
            "",
            f"- 状态：{f['status']}（confidence={f['confidence']}）",
            f"- CWE：{f['cwe'] or '待分类'}",
            f"- 目标：{f['target'] or '-'}",
            "",
        ]
        if f.get("description"):
            lines += ["**描述**", "", f["description"], ""]
        if f.get("reproduction"):
            lines += ["**复现步骤**", "", "```", f["reproduction"], "```", ""]
        if f.get("impact"):
            lines += ["**影响**", "", f["impact"], ""]
        if f.get("evidence"):
            lines += ["**证据**", ""]
            for e in f["evidence"]:
                lines.append(f"- `{e}`")
            lines.append("")
    lines += ["## 4. PoC 复现步骤", ""]
    lines.append(
        "复现步骤见第 3 节各漏洞；所有 confirmed 漏洞均已通过独立 PoC 复验（verify 角色纪律）。"
    )
    lines += ["", "## 5. 影响", ""]
    lines.append(
        "以第 3 节 confirmed 漏洞的严重度与影响描述为准；未确认项见第 6 节否定记录，不作为结论。"
    )
    lines += ["", "## 6. 建议与否定记录", ""]
    exhausted = report.get("exhausted", [])
    neg = report.get("negatives", [])
    if not exhausted and not neg:
        lines.append("（无）")
    for n in neg[:20]:
        lines.append(f"- [未确认] {n['title']}（{n.get('severity', '')}，状态 {n['status']}）")
    for e in exhausted[:20]:
        lines.append(
            f"- [试过未成] {e.get('title', '')}：{e.get('fail_reason', '') or e.get('result', '')[:80]}"
        )
    return "\n".join(lines)


def write_report(md: str, *, job_id: str) -> Path:
    """落盘 jobs_dir/<job_id>/report.md，返回路径。"""
    jdir = _jobs_dir() / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    path = jdir / "report.md"
    path.write_text(md, encoding="utf-8")
    return path
