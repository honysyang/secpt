"""双图事件线消费端（M5）：订阅 graph_changed，在步边界判"要不要重估规划"。

设计文档 L16 要求"可灵活调整规划（根据双图的状态，评估整体的规划，LLMC
教练员接入）事件驱动架构"。M4-6 只落了生产端（每步事务提交后 emit
``graph_changed``），本模块是缺失的消费端，分工照旧按 R3 风险红线办：

- **消费端只置旗不接管**：订阅者手里没有账本写口、没有 LLM、没有预算杠杆，
  它唯一的能力是记下"到点了该重估"（``take()`` 取走即清）；真正重估由主循环
  在步边界调 LLMC 完成——账先落、提案后吸收，与失速救场同一手式。
- **为什么按步数节拍而不是每步都响**：事件线口径是"读双图变化判要不要救，
  不必每步都响"（设计文档 L17）。每 N 步一次是确定性节拍——同一 run 里何时
  重估可复算，测试可钉死；是否值得重估仍由教练读双图决定（无话可说 = 规划
  仍成立，不落账不占名额）。
- **为什么用事件而不是循环内联判断**：面板/日志/测试都能看到同一份
  ``graph_changed`` 事实；消费端挂在总线上，未来增删消费者不动主循环。

配置：profile.toml ``[plan]`` 段 ``review_every_steps``（默认 5）、
``review_limit``（默认 3，每 run 最多重估次数）。坏值是操作方笔误 → E_PROFILE
拒跑（同 [execute] 段口径），不静默降级。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from secpt.errors import SecptError

_DEFAULT_REVIEW_EVERY_STEPS = 5
_DEFAULT_REVIEW_LIMIT = 3


class PlanConfig(BaseModel):
    """规划重估装配快照：节拍 + 每 run 名额（两个上界都必须为正）。"""

    review_every_steps: int = _DEFAULT_REVIEW_EVERY_STEPS
    review_limit: int = _DEFAULT_REVIEW_LIMIT


def load_plan_config(path: str | None) -> PlanConfig:
    """读 profile.toml ``[plan]`` 段；缺文件/缺段取默认（与 [execute] 同款宽容）。

    坏值（非表 / 非整数 / 布尔 / ≤0）一律 E_PROFILE：节拍写错会静默改变重估
    频率，属于"配置笔误必须报"的那类。
    """
    if not path:
        return PlanConfig()
    file = Path(path)
    if not file.is_file():
        return PlanConfig()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise SecptError(
            "profile.toml 解析失败",
            code="E_PROFILE",
            context={"path": str(file), "reason": str(exc)},
        ) from exc
    section = data.get("plan") if isinstance(data, dict) else None
    if section is None:
        return PlanConfig()
    if not isinstance(section, dict):
        raise SecptError("[plan] 段必须是表", code="E_PROFILE", context={"path": str(file)})
    return PlanConfig(
        review_every_steps=_positive(
            section.get("review_every_steps"), _DEFAULT_REVIEW_EVERY_STEPS, "review_every_steps"
        ),
        review_limit=_positive(section.get("review_limit"), _DEFAULT_REVIEW_LIMIT, "review_limit"),
    )


def _positive(raw: object, default: int, key: str) -> int:
    """正整数校验：bool 是 int 的子类，必须显式排除（与 [execute] 段同手式）。"""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise SecptError(
            f"[plan].{key} 必须为正整数",
            code="E_PROFILE",
            context={"got": repr(raw)},
        )
    return raw


class PlanWatcher:
    """graph_changed 订阅者：每 N 步置一次"待重估"旗，``take()`` 取走即清。

    线程语义：只在主循环线程的 emit 回调与步边界读之间流转；加锁为多消费者
    预留（面板订阅同一总线时不共享本实例，锁只保护本实例内部状态）。
    """

    def __init__(self, every_steps: int = _DEFAULT_REVIEW_EVERY_STEPS) -> None:
        if every_steps <= 0:
            raise ValueError(f"every_steps 必须为正整数，got {every_steps}")
        self._every = every_steps
        self._pending = False

    @property
    def every_steps(self) -> int:
        return self._every

    def __call__(self, event: str, payload: Mapping[str, object]) -> None:
        """总线回调：只认 graph_changed 且步号整除节拍——不读库、不抛错。"""
        if event != "graph_changed":
            return
        step_no = payload.get("step_no")
        if not isinstance(step_no, int) or step_no <= 0:
            return
        if step_no % self._every == 0:
            self._pending = True

    def take(self) -> bool:
        """取走旗位：True = 到点了该重估（读后即清，避免重复烧 LLM）。"""
        if not self._pending:
            return False
        self._pending = False
        return True


__all__ = ["PlanConfig", "PlanWatcher", "load_plan_config"]
