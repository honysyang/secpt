"""编排层汇总契约（结构化加固轮）：RunSummary / ChainResult。

背景：单链 / stages / frontier(team) / benchmark 四条执行路径各自拼 ad-hoc
dict 返回，字段集不一致（cmd_run 曾在 team 模式 `KeyError: 'entries'` 崩溃，
当时用 dag._fill_summary_contract 打补丁）。现收敛为**单点类型化契约**：

- 所有编排器统一 `RunSummary(...).to_dict()` 返回（对外仍是 dict，CLI/测试
  兼容不变）；缺省值即"该路径无此口径"（warroom 计数 / verify 计数等）
- chains 条目统一 ChainResult：单链填 entry，frontier/benchmark 填
  intent+asset_scope——两类字段在所有条目上**都存在**（缺省 -1/空串），
  消费方不再需要双形态分支判断

新增编排路径只需构造本契约；新增统计口径加字段（带默认值）而非新 dict 键。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ChainResult:
    """一条执行链 / 一条意图的结果（统一形态：两类编排都填满字段）。"""

    status: str = "failed"
    steps: int = 0
    role: str = ""
    entry: int = -1  # 单链/stages：入口下标；frontier/benchmark：-1
    intent: str = ""  # frontier/benchmark：意图标题；单链/stages：空串
    flag: str | None = None
    wall_s: float = 0.0
    depth: int = 0
    source: str = ""
    asset_scope: str = ""
    lane: str = "main"  # 执行通道：main 主路径 / logistics 后台后勤

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunSummary:
    """编排汇总（对外 dict 契约的单点定义，见模块 docstring）。"""

    job_id: str
    mode: str
    entries: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    chains: list[ChainResult] = field(default_factory=list)
    wall_s: float = 0.0
    open_remaining: int = 0
    warroom_assets: int = 0  # 历史口径（warroom 已退役：恒 0，保留字段兼容）
    warroom_attempts: int = 0
    findings: int = 0
    verified: int = 0
    # 扩展区：编排器自有附加信息（done_events / sub_jobs / ledger /
    # challenges_* 等），平铺进 dict 保持既有消费方（cmd_run 打印/测试断言）兼容
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "job_id": self.job_id,
            "mode": self.mode,
            "entries": list(self.entries),
            "flags": list(self.flags),
            "chains": [c.to_dict() if isinstance(c, ChainResult) else c for c in self.chains],
            "wall_s": self.wall_s,
            "open_remaining": self.open_remaining,
            "warroom_assets": self.warroom_assets,
            "warroom_attempts": self.warroom_attempts,
            "findings": self.findings,
            "verified": self.verified,
        }
        out.update(self.extras)
        return out
