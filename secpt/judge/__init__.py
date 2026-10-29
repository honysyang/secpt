"""裁判包（W3-2 蓝本 WF4）：profile 装配 + R0 判零 + R4 oracle + R1 重放 + R2/R3 + sweep。

五道流水线全部就位：R0 自证 / R4 oracle / R1 只读重放（M3-4）/ R2 双路径 +
R3 反证（M5）。judge 单向不 import tools：重放走注入式 ReplayExecutor。
"""

from secpt.judge.profile import GroundTruth, JudgeProfile, load_profile, note_for
from secpt.judge.r0_self import r0
from secpt.judge.r4_oracle import Match, r4
from secpt.judge.r23 import r2, r3
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
    "r2",
    "r3",
    "r4",
    "sweep",
]
