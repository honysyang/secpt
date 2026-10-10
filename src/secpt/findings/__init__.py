"""findings：漏洞发现的全生命周期管理（验证纪律核心）。

黑客逻辑：0day 的产出是**可复现的证据链**，不是字符串。Finding 必须区分
inferred（疑似）与 confirmed（已证实），且证据以指针引用（流量快照/文件/步骤），
对抗式复现（不同路径再触发一次）后才允许升级 confirmed。

层级：
    models.py   Finding / EvidenceRef / RetestRecord 数据模型
    store.py    FindingStore（SQLite，跨 job 存活，9 态分诊）
    tool.py     report_finding 结构化上报工具（Engine extra_tool 装配）
    verify.py   对抗式自检与复测（复用 Engine + evaluation 基建）
"""

from secpt.findings.models import EVIDENCE_ROLES, SEVERITIES, STATUSES, EvidenceRef, Finding

__all__ = [
    "EVIDENCE_ROLES",
    "SEVERITIES",
    "STATUSES",
    "EvidenceRef",
    "Finding",
]
