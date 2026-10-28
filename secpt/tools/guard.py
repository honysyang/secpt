"""Guard 分级闸（M3-1，§2.1 L102 / §3.6 规则 4 / §7 安全表 / WF3 拦截点）。

为什么闸在 registry 而不在 loop：蓝本把拦截点钉死为"工具调用前"，registry 是
全系统唯一的工具登记与 dispatch 咽点——外置工具、内嵌 agent_run 走同一个
入口，一道闸全覆盖（§3.6"Guard 同权"天然成立）。Guard 本身不落票不落账：
拒绝回执照常回主循环，audit 由 loop 带着合法 task_id 收口（registry 保持
ledger-free，黑板写入口唯 store/* 的红线不破）。

三项拍板（§67）：配置入 profile.toml [guard] 段（靶场档案按靶锁边界）；
HIGH 在 HITL（M3-2）到位前 fail-closed 一律拦（"没有 Guard 就没有合规
底线"的保守面）；缺 [guard] 段 = no-op，装配行为与 M1/M2 逐字节一致。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from secpt.errors import SecptError

ToolTier = Literal["high", "mid", "low"]
_VALID_TIERS: tuple[str, ...] = ("high", "mid", "low")

_E_GUARD_DENY = "E_GUARD_DENY"

# §3.6 规则 4：不可声明降级的保底 HIGH 档（内置，不随 [guard.tiers] 变）。
FLOOR_HIGH: frozenset[str] = frozenset({"agent_run"})


class GuardConfig(BaseModel):
    """Guard 装配快照：tier 声明表 + 白名单（两个都可选，无则闸只拦保底档）。"""

    tiers: dict[str, str] = Field(default_factory=dict)
    whitelist: tuple[str, ...] | None = None

    def tier_of(self, name: str) -> str:
        """工具的生效分级：保底档顶回声明（不可降级），未声明回落 low。"""
        if name in FLOOR_HIGH:
            return "high"
        return self.tiers.get(name, "low")

    def check(self, name: str) -> tuple[bool, str]:
        """闸门判定（§6.2 纯函数口径）：放行回 (True, tier)，拒绝回 deny 文案。

        先白名单（最小权限）后分级（HIGH fail-closed）：两级语义分开报，
        deny 文案直接作为拒绝小票 summary，可证可读。
        """
        if self.whitelist is not None and name not in self.whitelist:
            return False, (f"{_E_GUARD_DENY}: {name} 不在白名单（白名单制最小权限）")
        tier = self.tier_of(name)
        if tier == "high":
            return False, (
                f"{_E_GUARD_DENY}: {name} 为 HIGH 级动作，未获授权不放行"
                "（M3-1 fail-closed，HITL 弹窗 M3-2 接线）"
            )
        return True, tier


def load_guard_config(path: str | None) -> GuardConfig | None:
    """读 profile.toml 的 [guard] 段装配 GuardConfig；缺段/缺文件 → None（no-op）。

    三档语义（对齐 judge.load_profile 的拍板③手式）：
    - path 为 None 或文件不存在 → None（Guard 不上班）；
    - [guard] 段不存在 → None（拍板③：no-op，M1/M2 老库零影响）；
    - tier 声明值非法 → SecptError(code="E_PROFILE")（笔误拒跑有痕）。
    """
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise SecptError(
            "profile.toml 解析失败",
            code="E_PROFILE",
            context={"path": str(file), "reason": str(exc)},
        ) from exc
    guard_raw = data.get("guard")
    if not isinstance(guard_raw, dict):
        return None

    tiers_raw = guard_raw.get("tiers", {})
    if not isinstance(tiers_raw, dict):
        raise SecptError(
            "guard.tiers 必须是表",
            code="E_PROFILE",
            context={"path": str(file), "got": type(tiers_raw).__name__},
        )
    tiers: dict[str, str] = {}
    for name, tier in tiers_raw.items():
        if tier not in _VALID_TIERS:
            raise SecptError(
                f"guard.tiers[{name}] 非法分级 {tier!r}（可选 high/mid/low）",
                code="E_PROFILE",
                context={"path": str(file)},
            )
        tiers[str(name)] = str(tier)

    whitelist_raw = guard_raw.get("whitelist")
    whitelist: tuple[str, ...] | None = None
    if whitelist_raw is not None:
        if not isinstance(whitelist_raw, list):
            raise SecptError(
                "guard.whitelist 必须是清单",
                code="E_PROFILE",
                context={"path": str(file)},
            )
        whitelist = tuple(str(item) for item in whitelist_raw)

    return GuardConfig(tiers=tiers, whitelist=whitelist)


__all__ = ["FLOOR_HIGH", "GuardConfig", "ToolTier", "load_guard_config"]
