"""R1 重放裁判（蓝本 WF4 判4）：只读复跑比对；写操作直接悬置交 R2/R3。

为什么 judge 只认注入式执行器（拍板①）：分层红线是 judge 单向、不 import
tools——重放怎么跑（registry 只读门、Guard 同权过闸）是装配期的事，本层只见
一个 callable：给工具名+参数换一份新回执，``None`` = 写操作/未知（零执行）。
写操作与无票面锚的悬案由 :mod:`secpt.judge.r23` 的 R2 双路径 / R3 反证接手。

锚点判同（拍板）：R1 不比对"经验条目 command 与 args_json 是否逐字相同"——
command 是 LLM 自由文本，无法确定性对齐；改判"证据链同源"：经验条目 raw_ref
锚在 finding.evidence_ref 同一条证据上（Distiller 沉淀即锚、票盒热造三点同
链），同一锚即同一经验，命中判定 100% 确定。

纪律（蓝本 WF4 尾注）：永不 rerun 写操作（由 ReplayExecutor 返 None 机制化，
不靠自觉）；裁判不 throw 只改账——无结论就是悬置 + audit，账本层错误
（LedgerError 家族）原样上抛由 loop 的异常出口兜（sweep §9 同款）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from secpt.store import experience, ledger
from secpt.store.ledger import FindingRow

# 易变行（M5-25）：重放比对必须剔除，否则真实工具输出永远"不匹配"。
# nmap 的 raw 里有启动时间、耗时、延迟、argv 封面——两次运行必然不同；
# 直接全文相等会让 R1 在真实场景恒失败（单测用确定性假工具，暴露不出来）。
_VOLATILE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"  # 任何 ISO 日期时间
    r"|\d{2} [a-z]{3} \d{4} \d{2}:\d{2}:\d{2}"  # RFC 1123（M5-33：HTTP Date 头
    #   "Thu, 29 Oct 2026 16:14:20 GMT"——实测同 URL 两次抓取只差这一行，缺它
    #   R1 在真实 http 证据上恒不匹配，4 条合法 claim 全部悬置到 E_WALL）
    r"|starting nmap .* at "  # nmap 启动行
    r"|nmap done at "
    r"|scanned in [\d.]+ seconds"
    r"|^\$ argv\(",  # 命令行封面（执行器/镜像路径可能变）
    re.IGNORECASE,
)
# 行内延迟注记（"(0.0012s latency)"）：剥掉即可，不该丢掉整行证据。
_LATENCY = re.compile(r"\(\s*[\d.]+s?\s*latency\s*\)", re.IGNORECASE)


def stable_lines(text: str) -> list[str]:
    """把证据文本归一成"稳定行"：折叠空白、小写、剔除易变行。

    R1 的判据是"证据内容一致"，不是"文件逐字节相同"——判据必须对时间戳、
    耗时、命令行封面免疫，否则真实工具的两次运行永远不等。行内延迟注记
    （``(0.0012s latency)``）只剥离不丢行：端口证据要留下。
    """
    kept: list[str] = []
    for raw in text.splitlines():
        line = " ".join(_LATENCY.sub("", raw).split())
        if not line or _VOLATILE.search(line):
            continue
        kept.append(line.casefold())
    return kept


class ReplayOutcome(Protocol):
    """重放回执照面（ToolOutput 的结构子集）：judge 只认形状，不认 tools 类型。"""

    raw_ref: str
    exit_code: int | None
    summary: str


class ReplayExecutor(Protocol):
    """R1 重放执行器契约（拍板①注入式）：main 装配期绑定 registry.readonly_replay。

    形态是 ``__call__``（直接可调用）而非方法名——注入物是"就绪 callable"
    （registry.readonly_replay 的绑定方法本体），judge 不追问它挂谁的怀表。
    """

    def __call__(self, name: str, args: Mapping[str, str]) -> ReplayOutcome | None: ...


def _suspend(conn: sqlite3.Connection, finding: FindingRow, reason: str) -> None:
    """悬置落账（audit actor=judge）：有结论才改判，没结论就诚实记一行。"""
    ledger.audit(conn, finding.task_id, "judge", f"裁决 {finding.id[:8]}: 悬置（{reason}）")


def r1(
    conn: sqlite3.Connection,
    finding: FindingRow,
    replay: ReplayExecutor,
    environment: str | None,
) -> FindingRow | None:
    """R1 重放裁决：一只读票走完全程，五分支全部落账后返回。

    分支（对账拍板）：
    - 无票面证据源（锚没落在任何小票上，如 obs 锚/手铺证明）→ 悬置不裁决；
    - 写操作/未知工具（执行器返 None）→ 悬置（claimed 保持，交 R2/R3 接手）；
    - 重放失败（exit_code 空/非零、raw_ref 空、产物文件缺失）→ 悬置不裁决；
    - 新旧证据的**稳定行**一致（剔除时间戳/耗时/argv 封面）→ verified(method="rerun")
      + 经验升 verified 且 +1 票；
    - 不一致 → 经验降票（地板 0）且 claim 保持 claimed（减票不动判）。

    Args:
        environment: 经验库环境键；``None`` = 未装配经验键，裁决照走、票盒静默
            （评审口径：宁可少记账也不能编账）。

    Returns:
        被裁决的行（仅 verified(rerun) 分支，与 sweep 的 verdicts 口径一致）；
        其余分支一律 None（状态保持，audit 已可证）。
    """
    anchor = finding.evidence_ref
    if not anchor.strip():
        _suspend(conn, finding, "证据锚点为空，R1 无处起跑")
        return None
    # M5-31 取票排除 declare_finding 受理票：受理票的 raw_ref 恰好等于
    # evidence_ref（受理工具署名即锚点），按 raw_ref 倒序取"最近一张"永远取到
    # 它，于是 R1 永远看到"非只读，永不 rerun"——真实 run 实测一条 claim 被这样
    # 连续悬置 27 轮，R1 一次都没跑过。原始工具票（http_get/nmap…）才是
    # "证据怎么来的"那张票；锚上只有受理票 = 证据不是本 run 任何工具产的，
    # R1 无从重放，如实悬置交 R2/R3。
    ticket = conn.execute(
        "SELECT tool_name, args_json FROM tickets"
        " WHERE raw_ref = ? AND task_id = ? AND tool_name != 'declare_finding'"
        " ORDER BY rowid DESC LIMIT 1",
        (anchor, finding.task_id),
    ).fetchone()
    if ticket is None:
        _suspend(conn, finding, "证据锚未落在任何原始工具小票上（仅受理票/无票面），交 R2/R3")
        return None
    try:
        parsed: object = json.loads(str(ticket["args_json"]))
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if not isinstance(parsed, dict):
        _suspend(conn, finding, "小票参数不可解析，R1 无法重放")
        return None
    args: dict[str, str] = {str(k): str(v) for k, v in parsed.items()}
    outcome = replay(str(ticket["tool_name"]), args)
    if outcome is None:
        ledger.audit(
            conn,
            finding.task_id,
            "judge",
            f"裁决 {finding.id[:8]}: 悬置（R2/R3 接手：{ticket['tool_name']}"
            " 非只读，永不 rerun 写操作）",
        )
        return None
    original = Path(anchor)
    replayed = Path(outcome.raw_ref)
    stdout_ok = outcome.exit_code is not None and outcome.exit_code == 0
    if not stdout_ok or not outcome.raw_ref.strip():
        _suspend(conn, finding, f"R1 重放失败：{ticket['tool_name']} 未给出有效产物")
        return None
    if not (original.is_file() and replayed.is_file()):
        _suspend(conn, finding, "R1 重放失败：原证据或重放产物文件缺失（诚实锚点缺位）")
        return None
    try:
        original_lines = stable_lines(original.read_text(encoding="utf-8"))
        replayed_lines = stable_lines(replayed.read_text(encoding="utf-8"))
    except OSError:
        _suspend(conn, finding, "R1 重放失败：证据文件不可读")
        return None
    if not original_lines:
        _suspend(conn, finding, "R1 重放无法比对：原证据剔除易变行后为空")
        return None
    if original_lines == replayed_lines:
        row = ledger.set_finding_state(conn, finding.id, "verified", method="rerun")
        if environment is not None:
            experience.promote_claimed(conn, finding.task_id, environment, anchor)
        ledger.audit(
            conn,
            finding.task_id,
            "judge",
            f"裁决 {finding.id[:8]}: verified(rerun)"
            f"（重放一致：{len(original_lines)} 行稳定证据，剔除时间戳/耗时）",
        )
        return row
    if environment is not None:
        experience.decay_votes(conn, finding.task_id, environment, anchor)
    _suspend(conn, finding, "R1 重放不匹配：证据不稳，claimed 保持（经验已降票）")
    return None


__all__ = ["ReplayExecutor", "ReplayOutcome", "r1", "stable_lines"]
