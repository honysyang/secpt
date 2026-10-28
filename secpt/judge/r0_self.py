"""R0 自证判零（WF4 第一步）：纯代码、零 LLM 成本。

判据（拍板②+M3-3 §69 扩判）：锚两族——
- ``obs:<id>``：锚 observations 表的行（流量真相），行存在且 meta_json /
  snapshot_ref 非空才放行（snapshot 是真文件的指针，文件本身在 R1-R3 复查）。
- 文件锚（原判据不回退）：evidence_ref 指向的文件**存在且非空**。

空白锚 / 文件不存在 / 零字节 / obs 行缺失或空壳一律 rejected——渗透报告最大的
谎话就是"我说有洞但证据没存档"。为什么是"存在非空"级而非内容审核：内容真伪
是 R1-R3（复跑/双路/反面）的战场，R0 只拦"根本拿不出证据"这一类，边界必须
可一键复算。

为什么扩签 ``r0(conn, finding)``：obs 锚要查 observations 表，纯函数拿不到
行——签名连带 sweep 单调用方与 TestR0 测试同步演进（§69 记档）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from secpt.store.ledger import FindingRow


def r0(conn: sqlite3.Connection, finding: FindingRow) -> bool:
    """判零：True = 有直接证据放行 R4；False = 直接 rejected（由 sweep 落账）。"""
    ref = finding.evidence_ref.strip()
    if not ref:
        return False
    if ref.startswith("obs:"):
        row = conn.execute(
            "SELECT meta_json, snapshot_ref FROM observations WHERE id = ?", (ref[4:],)
        ).fetchone()
        if row is None:
            return False
        return bool(row[0]) and bool(row[1])
    path = Path(ref)
    return path.is_file() and path.stat().st_size > 0


__all__ = ["r0"]
