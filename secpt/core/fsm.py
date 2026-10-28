"""FSM 四态机（铁律 1：状态由显式状态机管，非法迁移抛错，不靠自觉）。

M1 只需要这条环：``ENTER → WORK →(WORK|EXIT|ABNORMAL)``，EXIT/ABNORMAL 是
终态（已收尾的 run 不复活——重开走新的 task/run， legislature 不改状态）。
迁移矩阵声明为常量表而非 if 链：表本身就是契约，一眼可审。

自迁（go 到当前态）是显式合法的幂等 no-op——WF2 的"干活自环"每一步都会发生。
非法迁移抛 :class:`~secpt.errors.SecptError` 且**状态保持原样**：宁可把矛盾
亮出来让上层决定，绝不悄悄 Self-heal 吞掉非法序列。
"""

from __future__ import annotations

from enum import Enum, auto

from secpt.errors import SecptError


class FSMState(Enum):
    """run 的生命周期四态（§9 异常出口图：进场 → 干活 → 出场/异常出口）。"""

    ENTER = auto()
    WORK = auto()
    EXIT = auto()
    ABNORMAL = auto()


# 迁移矩阵：键=当前态，值=允许去往的态（不含自迁；自迁统一在 go() 里放行）。
_TRANSITIONS: dict[FSMState, frozenset[FSMState]] = {
    FSMState.ENTER: frozenset({FSMState.WORK}),
    FSMState.WORK: frozenset({FSMState.WORK, FSMState.EXIT, FSMState.ABNORMAL}),
    FSMState.EXIT: frozenset(),
    FSMState.ABNORMAL: frozenset(),
}


class Fsm:
    """run 状态机载体：持有当前态，``go`` 做唯一迁移口。"""

    def __init__(self, state: FSMState = FSMState.ENTER) -> None:
        """进场默认 ENTER；显式给 ``state`` 是为了测试/复建任意起点的状态场景。"""
        self._state = state

    @property
    def state(self) -> FSMState:
        """当前态（只读：改状态的合法路径只有 go）。"""
        return self._state

    def go(self, target: FSMState) -> None:
        """唯一迁移口：合法则迁移；自迁为幂等 no-op；非法抛错且状态保持原样。"""
        if target is self._state:
            return
        if target not in _TRANSITIONS[self._state]:
            raise SecptError(
                f"非法迁移 {self._state.name} → {target.name}",
                context={"from_state": self._state.name, "to_state": target.name},
            )
        self._state = target


__all__ = ["FSMState", "Fsm"]
