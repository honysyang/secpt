"""R4 oracle 命中（WF4 第二步）：ground_truth 子串至指定即 verified(oracle)。

为什么是子串而非正则（拍板④）：M2 要最简可复算的裁判——pattern 是运维
同学在 profile.toml 里手写的一行证据关键词（如 "22/tcp open"），子串匹配
不区分大小写即可覆盖 nmap 输出的大小写漂移；正则的能力留给 M3 R1-R3。
多命中取清单首条；confidence 恒 None（无量化依据不编数）。
"""

from __future__ import annotations

from pydantic import BaseModel

from secpt.judge.profile import JudgeProfile
from secpt.store.ledger import FindingRow


class Match(BaseModel):
    """命中结果：吃到哪条 ground_truth（判 verified(oracle) 的背书账）。"""

    id: str
    pattern: str


def r4(finding: FindingRow, profile: JudgeProfile) -> Match | None:
    """oracle 裁决：命中 → verified(oracle) 依据；未命中/关闭 → None（不裁决）。

    返回 None 的语义是"轮到 R1-R3/M3"，与 rejected 严格区分——未命中不是
    反面证据，只是正面证据还没到。
    """
    if not profile.enabled:
        return None
    haystack = (finding.title + "\n" + finding.detail).lower()
    for gt in profile.ground_truth:
        if gt.pattern.lower() in haystack:
            return Match(id=gt.id, pattern=gt.pattern)
    return None


__all__ = ["Match", "r4"]
