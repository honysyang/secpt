"""关键参数配置（设计文档 L66）：步数线与失速阈值从 profile.toml 装配。

为什么单独一个模块而不是塞进 loop 构造参数默认值：文档钉死"关键参数通过配置
文件配置"，而配置解析属于装配层职责——主循环只认数值，不认 toml。三个值的
共同约束是"正整数"，坏值（0/负数/布尔/字符串）一律 E_PROFILE 拒跑：阈值写错
会静默改变"撞限/拉铃"的时机，属于必须报出来的配置笔误。

- ``[budget].max_steps``：主 LLM 步数线（默认 200，文档 L23）；
- ``[stall].failures``：连续工具失败阈值（默认 5，M1 失速计语义）；
- ``[stall].repeat_plans``：连续提出同一规划的阈值（默认 2，文档 L84 重复抑制）。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel

from secpt.errors import SecptError

_DEFAULT_MAX_STEPS = 200
_DEFAULT_STALL_FAILURES = 5
_DEFAULT_STALL_REPEAT_PLANS = 2


class LimitsConfig(BaseModel):
    """步数线与失速阈值快照（三个值都必须为正整数）。"""

    max_steps: int = _DEFAULT_MAX_STEPS
    stall_failures: int = _DEFAULT_STALL_FAILURES
    stall_repeat_plans: int = _DEFAULT_STALL_REPEAT_PLANS


def load_limits_config(path: str | None) -> LimitsConfig:
    """读 ``[budget]`` / ``[stall]`` 段；缺文件/缺段取默认（与 [execute]/[plan] 同款宽容）。"""
    if not path:
        return LimitsConfig()
    file = Path(path)
    if not file.is_file():
        return LimitsConfig()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise SecptError(
            "profile.toml 解析失败",
            code="E_PROFILE",
            context={"path": str(file), "reason": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise SecptError("profile.toml 顶层必须是表", code="E_PROFILE", context={"path": str(file)})
    budget = _section(data.get("budget"), "budget", file)
    stall_sec = _section(data.get("stall"), "stall", file)
    return LimitsConfig(
        max_steps=_positive(budget.get("max_steps"), _DEFAULT_MAX_STEPS, "budget.max_steps"),
        stall_failures=_positive(
            stall_sec.get("failures"), _DEFAULT_STALL_FAILURES, "stall.failures"
        ),
        stall_repeat_plans=_positive(
            stall_sec.get("repeat_plans"), _DEFAULT_STALL_REPEAT_PLANS, "stall.repeat_plans"
        ),
    )


def _section(raw: object, name: str, file: Path) -> dict[str, object]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SecptError(f"[{name}] 段必须是表", code="E_PROFILE", context={"path": str(file)})
    return raw


def _positive(raw: object, default: int, key: str) -> int:
    """正整数校验：bool 是 int 子类，必须显式排除（同 [execute]/[plan] 手式）。"""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise SecptError(
            f"[{key}] 必须为正整数",
            code="E_PROFILE",
            context={"got": repr(raw)},
        )
    return raw


__all__ = ["LimitsConfig", "load_limits_config"]
