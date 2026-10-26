"""信号级唤醒（WORKFLOW §4.1 v2）：事件账本的合流闸门。

两级事件分类——这是「一次心跳只醒一次」的分流模型：
- 信号级（唤醒）：账面状态变了，编排者必须醒来看一眼（任务建/转、
  计划改、finding 提出/裁决、人工指令、教练升档、模型切换）；
- 噪音级（只入账不唤醒）：evidence / clue / audit / 动作起止等过程
  流水，轮末汇聚统一消化——它们不改变「接下来该做什么」的答案。

三条纪律为何缺一不可（§4.1）：
1. 同轮合并：窗口内多信号只产生一个 WakeUp，醒来一次性消化全部；
2. finding 置顶：finding 类信号让编排前先裁决台账（wake.finding），
   编排者醒来先看台账再排活；
3. 兜底闹钟不废除：信号唤醒只处理「账面变化」，停滞检查等机械动作
   由 scheduler.heartbeat 承担，信号唤醒不替代兜底。

持久化水印 wakeup.json 存 mission 根目录（不存 state.meta）：
test_runtime 测 4 要求 die+resume 与对照跑的 final.json 逐字节一致
（含 meta），水印放进 meta 会污染比对；且两跑法的事件流逐心跳同构，
同一窗口判定 → 同一条唤醒路径 → 轨迹必然一致。水印损坏/丢失的失败
方向是多编排一轮（幂等），不是漏唤醒——故宁取 0 不猜。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from pa.store.writer import StoreWriter
from pa.store.view import StoreView

# 信号级：出现即置 dirty（账面状态变化，编排者必须醒来）
SIGNAL_TYPES: frozenset[str] = frozenset(
    {
        "mission.created",
        "task.created",
        "task.state_changed",
        "plan.created",
        "plan.revised",
        "finding.proposed",
        "finding.adjudicated",
        "human.directive",
        "restart.decided",
        "mentor.intervened",
        "model.switched",
    }
)

# finding 置顶子集：醒后先看台账再排活（§4.1 纪律 ②）
FINDING_SIGNALS: frozenset[str] = frozenset({"finding.proposed", "finding.adjudicated"})

# 噪音级 = EventType 全集 - SIGNAL_TYPES（task.dispatched / decision.made /
# action.started·finished·blocked / evidence.stored / clue.added /
# worker.returned / mentor.advisory·advisory_agg / budget.charged·warning /
# context.assembled / mission.finished）——只入账，不唤醒。

_WAKEUP_FILE = "wakeup.json"


@dataclass(frozen=True)
class WakeUp:
    """一次心跳的唤醒包（同轮合并：窗口内多信号压成一个包）。"""

    signals: tuple[str, ...] = field(default=())
    finding: bool = False


def _load(root: Path, head: int) -> int:
    """读持久化水印；缺失/损坏/越头一律取 0——失败方向是多醒一次。"""
    p = root / _WAKEUP_FILE
    try:
        wm = int(json.loads(p.read_text(encoding="utf-8"))["wm"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if wm < 0:
        return 0
    return min(wm, head)  # 越头防腐（账本可能被裁剪）


def collect(writer: StoreWriter) -> WakeUp:
    """扫描未消化窗口 (wm, head]，归出一个唤醒包（只读，不推进水位）。"""
    head = writer.head_seq
    wm = _load(writer.root, head)
    seen: list[str] = []
    finding = False
    if wm < head:
        view = StoreView(writer.root)
        for ev in view.events(from_seq=wm + 1):
            if ev.type not in SIGNAL_TYPES:
                continue  # 噪音只入账，已在其账本位
            if ev.type not in seen:
                seen.append(ev.type)
            if ev.type in FINDING_SIGNALS:
                finding = True
    return WakeUp(signals=tuple(seen), finding=finding)


def save(writer: StoreWriter) -> None:
    """水位推进到当前账头（原子写；无 fsync——水印只是省算力提示，
    掉电回滚的后果是窗口重扫 → 多醒一轮，幂等无害）。"""
    tmp = writer.root / (_WAKEUP_FILE + ".tmp")
    tmp.write_text(json.dumps({"wm": writer.head_seq}), encoding="utf-8")
    os.replace(tmp, writer.root / _WAKEUP_FILE)
