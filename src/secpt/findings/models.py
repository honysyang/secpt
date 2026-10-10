"""Finding 数据模型：证据链与确认分级。"""

from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field

# 严重度（4 档）与 9 态分诊状态机（对齐真实渗透 engagement 的分诊生命周期）
SEVERITIES = ("critical", "high", "medium", "low")
STATUSES = (
    "inferred",
    "in_progress",
    "confirmed",
    "resolved",
    "risk_accepted",
    "false_positive",
    "duplicate",
    "ignored",
)
# 证据角色：基线 / 直接证明 / 独立复现 / 辅助（对齐渗透报告惯例）
EVIDENCE_ROLES = ("baseline", "proof", "verification", "supporting")


def _new_id() -> str:
    return f"fnd-{uuid.uuid4().hex[:10]}"


class EvidenceRef(BaseModel):
    """一条证据指针（非内联文本——实体由流量层/文件系统承载）。"""

    kind: Literal["traffic", "file", "step", "quote"] = "step"
    ref: str = ""  # traffic: 快照ID；file: 路径；step: job_id:step；quote: 原文
    role: Literal["baseline", "proof", "verification", "supporting"] = "proof"


class Finding(BaseModel):
    """一个漏洞发现。

    确认纪律：创建时恒为 inferred；只有对抗式复现（verify.py 走不同路径
    独立再触发）成功才升级 confirmed。证伪和证实同等有价值——
    false_positive 也是成果，防止重复踩坑。
    """

    id: str = Field(default_factory=_new_id)
    title: str  # 一句话标题（含影响）
    vuln_class: str = ""  # 漏洞类别（自由文本；C1 映射 CWE）
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    status: Literal[
        "inferred",
        "in_progress",
        "confirmed",
        "resolved",
        "risk_accepted",
        "false_positive",
        "duplicate",
        "ignored",
    ] = "inferred"
    confidence: float = 0.5  # 发现者的把握（0-1）

    target: str = ""  # 目标（host/url，verify/retest 的输入）
    description: str = ""  # 漏洞描述
    reproduction: str = ""  # 最小复现步骤
    impact: str = ""  # 影响

    evidence: list[EvidenceRef] = Field(default_factory=list)
    cwe: str = ""  # CWE 编号+名称（C1 填充，如 CWE-347）
    cve_candidates: list[str] = Field(default_factory=list)  # 可能相关的已知 CVE（C2 去重）

    first_seen: dict = Field(default_factory=dict)  # {job_id, step, ts}
    reproduced_by: list[dict] = Field(default_factory=list)  # [{job_id, verdict, ts}]
    job_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    evidence_version: int = 1  # 证据版本（C1 报告一致性乐观并发）

    def touch(self) -> None:
        self.updated_at = time.time()

    def add_evidence(self, ref: EvidenceRef) -> None:
        """追加证据；内容变化 bump evidence_version（报告一致性不变量）。"""
        key = (ref.kind, ref.ref, ref.role)
        existing = {(e.kind, e.ref, e.role) for e in self.evidence}
        if key not in existing:
            self.evidence.append(ref)
            self.evidence_version += 1
            self.touch()
