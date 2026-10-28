"""裁判包（W3-2 蓝本 WF4）：profile 装配 + R0 判零 + R4 oracle + sweep 收口。

M3-4 起加判票票盒：R1（只读重放 + 经验判票）由 r123 承载，R2/R3 为 M5 图纸位。
"""

from secpt.judge.profile import GroundTruth, JudgeProfile, load_profile, note_for
from secpt.judge.r0_self import r0
from secpt.judge.r4_oracle import Match, r4
from secpt.judge.r123 import ReplayExecutor, ReplayOutcome, r1
from secpt.judge.sweep import sweep

__all__ = [
    "GroundTruth",
    "JudgeProfile",
    "Match",
    "ReplayExecutor",
    "ReplayOutcome",
    "load_profile",
    "note_for",
    "r0",
    "r1",
    "r4",
    "sweep",
]
