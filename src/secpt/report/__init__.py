"""report：报告交付层。

- cwe.py      CWE 映射（LLM 分类 + 内置字典校验）
- delivery.py 报告生成（md/md-zip/json）+ 证据一致性快照
"""

from secpt.report.cwe import CWE_DICT, classify, format_cwe
from secpt.report.delivery import ReportDelivery

__all__ = ["CWE_DICT", "ReportDelivery", "classify", "format_cwe"]
