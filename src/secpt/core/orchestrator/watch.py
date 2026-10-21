"""Sentinel 值守哨兵（职名 SNT）：确定性结算与触发器，零 LLM（v4）。

设计出处：工作循环形态 v4——"LLM 只做必须判断的事，其余全是确定性代码"。
值守层是常驻心脏：结算一切事件（证据落库 / 预算记账 / 失败分类），评估触发器，
决定何时唤醒 Commander。主智能体反而是被唤醒的。

职责一：失败类型化分类（替代笼统的 exhausted，直接决定流转方向）：
    tool_failure   环境/工具问题      → 值守层自动重试，不惊动 Commander
    budget_exhausted 步数/墙钟烧尽    → 硬止损（Coach 缩任务注入已有）
    stuck          止损三档走完仍无进展 → 唤醒 Coach
    negative       实验成功执行但结果为负 → 信念更新（正常信息，不是失败）
    success        达成完成判据

职责二：四类触发器（从便宜到贵；stall/contradiction 依赖假设树）：
    budget_milestone  总预算烧到 1/3、1/2、3/4（墙钟或意图配额，先到先算）
    phase_gate        当前阶段判据达成或证伪（全子目标结算）
    stall             N 轮无信念增量（无假设状态迁移且无意图 done——不是无产出）
    consolidation     巩固定时器：每固定轮数强制唤醒做内务整理
矛盾报警（新证据与高置信假设冲突）依赖 intel 冲突检测，暂缺，留 TODO。

有状态：里程碑只报一次；stall 需要"距上次唤醒的进度标记"。全部可重置。
"""

from __future__ import annotations

from dataclasses import dataclass

# 失败类型（Watch 的结算输出；frontier 据此流转）
TOOL_FAILURE = "tool_failure"
BUDGET_EXHAUSTED = "budget_exhausted"
STUCK = "stuck"
NEGATIVE = "negative"
SUCCESS = "success"
CAPABILITY_GAP = "capability_gap"

_CAPABILITY_HINTS = (
    "没有可用工具",
    "缺少工具",
    "工具不存在",
    "需要凭据",
    "无权限",
    "command '",
    " 不存在",
    "request_tools",
)


@dataclass
class WakeSignal:
    kind: str  # budget_milestone | phase_gate | stall | consolidation
    detail: str = ""
    priority: int = 5

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "priority": self.priority}


def classify_failure(status: str, fail_reason: str = "") -> str:
    """引擎结果 → 类型化失败。确定性规则，无 LLM。"""
    s = (status or "").lower()
    why = (fail_reason or "").lower()
    if s in ("success", "done"):
        return SUCCESS
    if s == "stalled":
        return STUCK
    if s in ("step_exceeded", "wallclock_exceeded", "budget_exceeded"):
        return BUDGET_EXHAUSTED
    if s == "error":
        low = (fail_reason or "").lower()
        if any(h in low for h in _CAPABILITY_HINTS):
            return CAPABILITY_GAP
        return TOOL_FAILURE
    # 正常结束但未达成完成判据：实验成功执行、结果为负——是信息不是故障
    if s in ("failed", "exhausted") and "stall" not in why:
        return NEGATIVE
    return NEGATIVE


def capability_gap(fail_reason: str) -> bool:
    """产出是否在要一个不存在的工具/凭据（能力缺口 → 需求清单）。"""
    low = (fail_reason or "").lower()
    return any(h in low for h in _CAPABILITY_HINTS)


# ── 作战地图：对方反应（防御信号）的确定性检测 ──
# 封禁/限流/WAF/蜜罐特征——命中说明"打不动"可能不是技术问题，是防守方已反应。
# 轻量启发式：只做提示（落未消化证据，Commander 裁决），不自动改变策略。
_DEFENSIVE_PATTERNS = (
    "403 forbidden",
    "429 too many requests",
    "access denied",
    "blocked by waf",
    "waf",
    "rate limit",
    "banned",
    "captcha",
    "拒绝访问",
    "请求过于频繁",
    "已被封禁",
    "蜜罐",
    "honeypot",
)


_OUTPUT_GAP_PATTERNS = (
    "command not found",
    "命令 '",
    "不存在。插件",
    "未安装",
    "no such file or directory",
    "permission denied",
    "需要凭据",
    "没有可用工具",
    "request_tools",
)


def detect_capability_gaps(outputs: list[str]) -> list[str]:
    """从工具输出检测能力缺口（缺工具/缺凭据/缺权限）——需求清单的原料。

    与 fail_reason 口径互补：真实运行里缺失工具表现为 bash/插件的错误输出
    （引擎不因此终止），settle 级 fail_reason 扫描抓不到。
    """
    found: list[str] = []
    low_all = "\n".join((o or "").lower() for o in outputs or [])
    for pat in _OUTPUT_GAP_PATTERNS:
        if pat in low_all:
            found.append(pat)
    return found


def detect_defensive_signals(outputs: list[str], *, min_hits: int = 3) -> list[str]:
    """从近期工具输出中检测对方防御反应。

    返回命中的特征清单（去重）；同一特征在多个输出反复出现才计——
    单次 403 可能只是目录不存在，反复 403 + rate limit 才是封禁信号。
    """
    hits: dict[str, int] = {}
    for text in outputs or []:
        low = (text or "").lower()
        seen_here: set[str] = set()
        for pat in _DEFENSIVE_PATTERNS:
            if pat in low and pat not in seen_here:
                seen_here.add(pat)
                hits[pat] = hits.get(pat, 0) + 1
    return [pat for pat, n in hits.items() if n >= min_hits]


class Watch:
    """常驻触发器状态机（纯内存；重启后里程碑重新武装——多唤醒一次无害）。"""

    def __init__(
        self,
        *,
        max_intents: int = 0,
        consolidate_every: int = 6,
        stall_rounds: int = 2,
    ) -> None:
        self.max_intents = max(0, int(max_intents))
        self.consolidate_every = max(0, int(consolidate_every))
        self.stall_rounds = max(1, int(stall_rounds))
        self._milestones_fired: set[int] = set()  # 已触发的预算里程碑（30/50/75%）
        self._rounds_since_consolidation = 0
        self._progress_marker = 0  # 上次评估时的进度（done 意图数 + 假设终态数）

    # ---------------- 触发器 ----------------

    def budget_signals(
        self, *, elapsed_s: float, total_wall_s: float, used_intents: int
    ) -> list[WakeSignal]:
        """预算里程碑：墙钟或意图配额先到先算；每个阈值只报一次。"""
        signals: list[WakeSignal] = []
        fractions: list[tuple[int, float]] = []
        if total_wall_s and total_wall_s > 0:
            fractions.append((min(99, int(elapsed_s / total_wall_s * 100)), 0.0))
        if self.max_intents:
            fractions.append((min(99, int(used_intents / self.max_intents * 100)), 0.0))
        if not fractions:
            return signals
        pct = max(f for f, _ in fractions)
        for threshold in (30, 50, 75):
            if pct >= threshold and threshold not in self._milestones_fired:
                self._milestones_fired.add(threshold)
                signals.append(
                    WakeSignal(
                        kind="budget_milestone",
                        detail=f"预算已消耗约 {pct}%（阈值 {threshold}%），强制重估方向",
                        priority=7,
                    )
                )
        return signals

    def consolidation_signal(self) -> WakeSignal | None:
        """巩固定时器：每 N 轮强制唤醒一次做内务（无论有无其他触发器）。"""
        if not self.consolidate_every:
            return None
        self._rounds_since_consolidation += 1
        if self._rounds_since_consolidation >= self.consolidate_every:
            self._rounds_since_consolidation = 0
            return WakeSignal(
                kind="consolidation",
                detail="巩固定时器：内务整理（信念对账/死任务修剪/文档重写）",
                priority=4,
            )
        return None

    def stall_signal(self, *, done_intents: int, tree_transitions: int) -> WakeSignal | None:
        """停滞报警：连续 N 次评估无信念增量（不是无产出）。

        进度 = done 意图数 + 假设终态数（confirmed/refuted/dead/refined）。
        产出文件/工具输出不算——同一端口扫三遍不是增益。
        """
        progress = int(done_intents) + int(tree_transitions)
        fired: WakeSignal | None = None
        if progress == self._progress_marker and progress >= 0:
            self._stall_rounds_seen = getattr(self, "_stall_rounds_seen", 0) + 1
            if self._stall_rounds_seen >= self.stall_rounds:
                fired = WakeSignal(
                    kind="stall",
                    detail=f"连续 {self._stall_rounds_seen} 次评估无信念增量（done={done_intents}，"
                    f"假设迁移={tree_transitions}）",
                    priority=6,
                )
                self._stall_rounds_seen = 0  # 触发后重新计数（迟滞）
        else:
            self._stall_rounds_seen = 0
        self._progress_marker = progress
        return fired

    def reset_stall(self) -> None:
        self._stall_rounds_seen = 0
