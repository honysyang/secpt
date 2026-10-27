"""状态机：管理 Agent 执行状态流转。

状态流转：
    IDLE → PLANNING → EXECUTING → EVALUATING → COMPLETED
                    ↓
                FAILED (可重试 → IDLE)
                WAITING_INPUT (用户输入 → EXECUTING)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class State(Enum):
    """执行状态。"""

    IDLE = auto()
    PLANNING = auto()
    EXECUTING = auto()
    EVALUATING = auto()
    WAITING_INPUT = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass
class StateContext:
    """状态上下文，随状态流转传递。"""

    task: str = ""
    plan: Any = None
    results: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class StateMachine:
    """有限状态机。"""

    # 合法状态转换
    TRANSITIONS: dict[State, set[State]] = {
        State.IDLE: {State.PLANNING, State.EXECUTING},
        State.PLANNING: {State.EXECUTING, State.FAILED},
        State.EXECUTING: {State.EVALUATING, State.WAITING_INPUT, State.FAILED, State.COMPLETED},
        State.EVALUATING: {State.EXECUTING, State.COMPLETED, State.FAILED},
        State.WAITING_INPUT: {State.EXECUTING, State.FAILED},
        State.FAILED: {State.IDLE},  # 重试
        State.COMPLETED: {State.IDLE},  # 新任务
    }

    def __init__(self) -> None:
        self.state = State.IDLE
        self.context = StateContext()
        self._history: list[State] = []

    def transition(self, new_state: State) -> None:
        """状态转换。

        Args:
            new_state: 目标状态

        Raises:
            ValueError: 非法状态转换
        """
        allowed = self.TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"非法状态转换: {self.state.name} → {new_state.name}"
            )
        self._history.append(self.state)
        self.state = new_state

    def can_transition(self, new_state: State) -> bool:
        """检查是否可以转换到目标状态。"""
        return new_state in self.TRANSITIONS.get(self.state, set())

    def reset(self) -> None:
        """重置状态机。"""
        self.state = State.IDLE
        self.context = StateContext()
        self._history.clear()

    @property
    def history(self) -> list[str]:
        """返回状态历史。"""
        return [s.name for s in self._history]
