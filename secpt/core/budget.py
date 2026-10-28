"""步数线预算（开发文档 §9 双引擎对表："管钱不判好坏"）。

为什么是纯函数：预算判断必须是"可复核的算术"，不含任何可变状态——同一组入参
永远同一个答案。主循环只消费这里的布尔；撞限之后"走异常出口、怎么落账"是 loop
的语义（抛错归属 loop，见 E_BUDGET_EXHAUSTED），本层只回答"还花得起吗"。

参数从 harness 配置可调（max_steps 默认 200 / max_tools_per_step 默认 4），
不硬编码在函数里——调用方传值，函数只管判断。
"""

from __future__ import annotations


def try_spend(max_steps: int, step_no: int, *, cost: int = 1) -> bool:
    """判断"做完第 ``step_no`` 步（含本次要付的 ``cost``）"是否仍在步数线内。

    语义：本次要兑现的是步号 ``step_no..step_no+cost-1`` 共 ``cost`` 个步位，
    最后一个步位仍 ≤ ``max_steps`` 即 True（``try_spend(200, 200)`` 为 True：
    第 200 步自身还在线内）。越线一律 False，绝不抛错——"预算不足返回 False
    不抛错"是 §11 W1-6 的验收语义，撞限后怎么落账走异常出口是 loop 的语义。

    非法入参（非正步号/负成本/非正上限）保守判 False：宁可拦下也不放行脏预算。
    """
    if max_steps <= 0 or step_no <= 0 or cost < 0:
        return False
    return step_no + cost - 1 <= max_steps


def try_tool_calls(count: int, limit: int) -> bool:
    """判断一步内点名 ``count`` 个工具调用是否在可配置上限内（默认 4）。

    ``count=0``（纯思考不落工具的 done 步）合法。超限 False，不抛错。
    """
    if limit < 0 or count < 0:
        return False
    return count <= limit


__all__ = ["try_spend", "try_tool_calls"]
