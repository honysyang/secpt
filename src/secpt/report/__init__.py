"""report：报告交付层。

- cwe.py      CWE 映射（LLM 分类 + 内置字典校验）
- delivery.py 报告生成（md/md-zip/json）+ 证据一致性快照
- generator.py（legacy）WorldModel 时代的旧渲染，被 test_phase3 依赖，保留
"""

from secpt.report.cwe import CWE_DICT, classify, format_cwe
from secpt.report.delivery import ReportDelivery
from secpt.report.generator import Finding, Report, ReportGenerator

__all__ = [
    "CWE_DICT",
    "Finding",
    "Report",
    "ReportDelivery",
    "ReportGenerator",
    "classify",
    "format_cwe",
]
