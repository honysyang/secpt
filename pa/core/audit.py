"""出口审计（M3）：evidence 完整性校验进入交付门（WORKFLOW §6-2 / §7 M3）。

问题原型：thrash 无出口、「看不出进展却不说」——收口前把全部 finding 的
证据链一次性核验并随 mission.audit 事件落账，交付物（report.md）可复算
出同一结论。设计口径：

- 纯检查：audit_refs 只读账本投影 + 证据盒，无副作用、确定性（同一
  state + 同一证据目录重复核验逐字段一致，报告重算不读旧事件值）；
- 只落账不投影：mission.audit 不进 apply_event 投影表（§5.3 投影表之外
  的类型只落账本）——审计结论是收口时刻的快照，live 不依赖它做任何决策；
  单点挂 _finish（交付门）：先 mission.audit 再 mission.finished，账本
  顺序读起来「审计在先、收口在后」；
- 幂等：idem 键 ma:{mission_id}，同 mission 重复触发不重复落账；
- 验收面：confirmed 却带断链引用 = 账链漂移信号，写入 confirmed_broken
  单列——裁决口径（≥1 条可核验即 confirmed）允许其他引用断链，审计
  把它显式亮出来而不是吞掉（可观测红线）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pa.adjudicate import finding_id
from pa.infra import evidence
from pa.infra.log import get_logger
from pa.models import MissionState
from pa.store.writer import StoreWriter

logger = get_logger("secpt.audit")


def audit_refs(state: MissionState, root: Path) -> dict[str, Any]:
    """对全部 findings 的 evidence_refs 做完整性核验，返回审计摘要。

    返回字段：findings（提案数）、refs（引用总数）、verified（可核验）、
    broken（断链引用明细 [{"finding": fid, "claim": …, "refs": [...]}]）、
    confirmed_broken（裁决 confirmed 但含断链引用的 fid 列表）。
    """
    verified = 0
    total = 0
    broken: list[dict[str, Any]] = []
    confirmed_broken: list[str] = []
    for f in state.findings:
        fid = finding_id(f)
        bad = [ref for ref in f.evidence_refs if not _ok(root, ref)]
        for ref in f.evidence_refs:
            total += 1
            if _ok(root, ref):
                verified += 1
        if bad:
            broken.append({"finding": fid, "claim": f.claim, "refs": bad})
        adj = state.adjudications.get(fid)
        if adj is not None and adj.verdict == "confirmed" and bad:
            confirmed_broken.append(fid)
    return {
        "findings": len(state.findings),
        "refs": total,
        "verified": verified,
        "broken": broken,
        "confirmed_broken": confirmed_broken,
    }


def _ok(root: Path, ref: str) -> bool:
    """单条引用核验（check 语义包裹，保持 audit_refs 可读）。"""
    return evidence.verify(root, ref)


def exit_audit(state: MissionState, writer: StoreWriter) -> dict[str, Any]:
    """交付门审计：核验 → mission.audit 事件落账，返回摘要供报告复算。

    幂等命中（同 mission 已审计过）返回摘要但不重复落账——resume-finished
    路径不走 _finish，二跑不会到这里；保留防御零成本。
    """
    summary = audit_refs(state, writer.root)
    ev = writer.append("mission.audit", "audit", summary, f"ma:{state.mission_id}")
    if ev is None:
        logger.info("出口审计幂等命中：本 mission 已审计过，跳过重复落账")
    return summary
