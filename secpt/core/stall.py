"""失速计（§9 双引擎对表：连续 N 步无实质进展 → 拉铃）。

M1 的"实质进展"判据就一个：本步工具回执是否 exit_code==0（E_TOOL_FAIL 照落
小票 + 失速计 +1）。"规划无实质变化 / 连续判零"的算术判重是 M2 失速+LLMC 的
活，这里不预实现。

失败-拉铃-出口的顺序契约：拉铃只报告 True 不落账——落账（小票/水位线）与
E_STALL 异常出口的顺序由主循环保证（先账后铃，绝不 pre-commit 发未落账的铃）。
"""

from __future__ import annotations


class StallCounter:
    """连续失败计数器：成功清零，数满 N 报铃；绝对连续，中间无"宽宥"。"""

    def __init__(self, n: int = 5) -> None:
        if n <= 0:
            raise ValueError("失速阈值 n 必须为正整数")
        self._n = n
        self._streak = 0

    def observe(self, *, ok: bool) -> bool:
        """记录一次结果：成功清零连败；失败累加，数满 N 返回 True（拉铃）。"""
        self._streak = 0 if ok else self._streak + 1
        return self._streak >= self._n


__all__ = ["StallCounter"]
