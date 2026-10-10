"""报告生成：从 WorldModel 提取图数据 → 文本/Markdown 报告。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Finding:
    """一条安全发现。"""

    title: str
    severity: str          # critical / high / medium / low / info
    description: str
    evidence: list[str] = field(default_factory=list)
    remediation: str = ""


@dataclass
class Report:
    """渗透测试报告。"""

    title: str
    target: str
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)


class ReportGenerator:
    """从 WorldModel 数据生成报告。"""

    def __init__(self, worldmodel) -> None:
        self._wm = worldmodel

    async def generate(
        self,
        target: str,
        objective: str = "",
        title: str = "渗透测试报告",
    ) -> Report:
        """生成报告。"""
        # 收集主机实体
        hosts: list[dict] = []
        # 从图里查所有 host 类型实体
        all_neighbors = await self._collect_all_entities()
        for ent in all_neighbors:
            if ent.get("etype") == "host":
                hosts.append(ent)

        # 从端口生成发现
        findings: list[Finding] = []
        for ent in all_neighbors:
            if ent.get("etype") == "port":
                props = ent.get("props", {})
                port = props.get("port", "?")
                service = props.get("service", "unknown")
                findings.append(Finding(
                    title=f"开放端口 {port}/{props.get('protocol', 'tcp')}",
                    severity="info",
                    description=f"服务：{service} {props.get('version', '')}",
                    evidence=[f"port.{port}"],
                ))

        # 汇总
        summary = f"目标 {target}，发现 {len(hosts)} 台主机，{len(findings)} 个开放端口/服务。"
        if objective:
            summary = f"目标：{objective}。" + summary

        return Report(
            title=title,
            target=target,
            summary=summary,
            findings=findings,
            hosts=hosts,
        )

    async def _collect_all_entities(self) -> list[dict]:
        """收集图中所有实体（简化：从 store 的邻接关系遍历）。"""
        # 简化实现：从已知证据反查
        # 实际应从 WorldModel 提供全量查询接口
        return []

    def to_markdown(self, report: Report) -> str:
        """转 Markdown 格式。"""
        lines = [
            f"# {report.title}",
            "",
            f"**目标**: {report.target}",
            "",
            "## 摘要",
            "",
            report.summary,
            "",
            "## 发现",
            "",
        ]
        for f in report.findings:
            lines.append(f"### {f.title} [{f.severity}]")
            lines.append(f.description)
            if f.evidence:
                lines.append(f"**证据**: {', '.join(f.evidence)}")
            if f.remediation:
                lines.append(f"**修复建议**: {f.remediation}")
            lines.append("")

        lines.append("## 主机")
        lines.append("")
        for h in report.hosts:
            props = h.get("props", {})
            lines.append(f"- **{props.get('ip', '?')}** ({props.get('hostname', '')}) OS: {props.get('os', 'unknown')}")

        return "\n".join(lines)

    def to_text(self, report: Report) -> str:
        """转纯文本格式。"""
        lines = [
            f"{'='*60}",
            f"  {report.title}",
            f"{'='*60}",
            f"目标: {report.target}",
            f"",
            f"[摘要] {report.summary}",
            f"",
            f"[发现] ({len(report.findings)} 条)",
        ]
        for f in report.findings:
            lines.append(f"  [{f.severity:8s}] {f.title}")
            lines.append(f"             {f.description[:80]}")

        lines.append(f"")
        lines.append(f"[主机] ({len(report.hosts)} 台)")
        for h in report.hosts:
            props = h.get("props", {})
            lines.append(f"  {props.get('ip', '?')}  {props.get('hostname', '')}  {props.get('os', '')}")

        lines.append(f"{'='*60}")
        return "\n".join(lines)


__all__ = ["Finding", "Report", "ReportGenerator"]
