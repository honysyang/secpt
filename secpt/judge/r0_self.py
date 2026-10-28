"""R0 自证判零（WF4 第一步）：纯代码、零 LLM 成本。

判据（拍板②）：evidence_ref 指向的文件**存在且非空**才放行 R4；空白锚 /
文件不存在 / 零字节一律 rejected——渗透报告最大的谎话就是"我说有洞但证据
没存档"。为什么是文件级而非内容审核：内容真伪是 R1-R3（复跑/双路/反面）的
战场，R0 只拦"根本拿不出证据"这一类，边界必须可一键复算。
"""

from __future__ import annotations

from pathlib import Path

from secpt.store.ledger import FindingRow


def r0(finding: FindingRow) -> bool:
    """判零：True = 有直接证据放行 R4；False = 直接 rejected（由 sweep 落账）。"""
    ref = finding.evidence_ref.strip()
    if not ref:
        return False
    path = Path(ref)
    return path.is_file() and path.stat().st_size > 0


__all__ = ["r0"]
