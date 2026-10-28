"""W1-6 三件套 contract test：步数线预算 / FSM 四态机 / 失速计 N=5。

全部纯函数/纯逻辑直测（开发文档 §10："主循环/预算器 | 纯函数直测"）；
与主循环的接线测试见 test_loop.py 的异常出口用例。
"""

from __future__ import annotations

import pytest

from secpt.core import budget, fsm, stall
from secpt.errors import SecptError

# ---- 步数线：try_spend / try_tool_calls（预算不足 False，不抛错）----------------


def test_try_spend_within_budget_true() -> None:
    assert budget.try_spend(max_steps=200, step_no=1) is True
    assert budget.try_spend(max_steps=200, step_no=200) is True  # 第 200 步仍在线内


def test_try_spend_exhausted_false_no_raise() -> None:
    """步号越过步数线 = 预算不足：返回 False，绝不抛错（撞限的抛错归属 loop）。"""
    assert budget.try_spend(max_steps=200, step_no=201) is False
    assert budget.try_spend(max_steps=3, step_no=4) is False


def test_try_spend_cost_param() -> None:
    assert budget.try_spend(max_steps=5, step_no=4, cost=2) is True
    assert budget.try_spend(max_steps=5, step_no=5, cost=2) is False


def test_try_tool_calls_within_limit_true() -> None:
    assert budget.try_tool_calls(count=0, limit=4) is True  # 0 个调用（done 步）合法
    assert budget.try_tool_calls(count=4, limit=4) is True


def test_try_tool_calls_over_limit_false() -> None:
    assert budget.try_tool_calls(count=5, limit=4) is False


def test_try_spend_negative_step_invalid_false() -> None:
    assert budget.try_spend(max_steps=200, step_no=0) is False


# ---- FSM 四态机：显式迁移、非法迁移即抛错 --------------------------------------


def test_fsm_happy_path_enter_work_exit() -> None:
    machine = fsm.Fsm()
    assert machine.state == fsm.FSMState.ENTER
    machine.go(fsm.FSMState.WORK)
    machine.go(fsm.FSMState.WORK)  # WF2 多步：干活自环合法（不推进状态的那类迁移）
    machine.go(fsm.FSMState.EXIT)
    assert machine.state == fsm.FSMState.EXIT


def test_fsm_work_to_abnormal_allowed() -> None:
    machine = fsm.Fsm()
    machine.go(fsm.FSMState.WORK)
    machine.go(fsm.FSMState.ABNORMAL)
    assert machine.state == fsm.FSMState.ABNORMAL


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (fsm.FSMState.ENTER, fsm.FSMState.EXIT),  # 没干活就出场
        (fsm.FSMState.ENTER, fsm.FSMState.ABNORMAL),
        (fsm.FSMState.EXIT, fsm.FSMState.WORK),  # 已收尾不复活
        (fsm.FSMState.ABNORMAL, fsm.FSMState.WORK),
        (fsm.FSMState.WORK, fsm.FSMState.ENTER),  # 干活中途二次进场
    ],
)
def test_fsm_illegal_transition_raises(start: fsm.FSMState, target: fsm.FSMState) -> None:
    machine = fsm.Fsm(start)  # EXIT/ABNORMAL 是终态，只能从构造进场，不能 go 进去
    with pytest.raises(SecptError, match="非法迁移"):
        machine.go(target)
    assert machine.state == start  # 迁移失败：状态保持原样


def test_fsm_self_transition_is_noop() -> None:
    """同态自迁：显式合法但不改状态（幂等）。"""
    machine = fsm.Fsm()
    machine.go(fsm.FSMState.ENTER)
    assert machine.state == fsm.FSMState.ENTER


# ---- 失速计：连续 N=5 拉铃 -----------------------------------------------------


def test_stall_trips_at_n5() -> None:
    counter = stall.StallCounter(n=5)
    for _ in range(4):
        assert counter.observe(ok=False) is False
    assert counter.observe(ok=False) is True  # 连续第 5 次失败 → 拉铃


def test_stall_resets_on_success() -> None:
    """连续性：一次成功（exit_code==0）把连败清零，重新计满 5。"""
    counter = stall.StallCounter(n=5)
    for _ in range(3):
        counter.observe(ok=False)
    assert counter.observe(ok=True) is False  # 未拉铃且清零
    for _i in range(4):
        assert counter.observe(ok=False) is False
    assert counter.observe(ok=False) is True  # 重新数满 5


def test_stall_success_alone_never_trips() -> None:
    counter = stall.StallCounter(n=2)
    for _ in range(10):
        assert counter.observe(ok=True) is False


def test_stall_n_configurable() -> None:
    counter = stall.StallCounter(n=2)
    assert counter.observe(ok=False) is False
    assert counter.observe(ok=False) is True


def test_stall_n_must_be_positive() -> None:
    with pytest.raises(ValueError, match="n"):
        stall.StallCounter(n=0)
