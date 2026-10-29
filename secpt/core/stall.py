"""失速计（§9 双引擎对表：连续 N 步无实质进展 → 拉铃）。

两个计数口径（都是"连续"语义，中间一破即清零）：
- **失败计**（M1）：本步工具回执是否 exit_code==0，连续失败的步数满 N 拉铃；
- **重复规划计**（M5，设计文档 L84 "重复抑制"）：同一段非空 plan_updates
  连续提出满 repeat_n 次拉铃——同输入相似输出是"原地打转"的早期信号，
  比等 5 次工具失败更早。空 updates 不算重复（模型没提规划 ≠ 重复提）。

失败-拉铃-出口的顺序契约：拉铃只报告 True 不落账——落账（小票/水位线）与
E_STALL 异常出口的顺序由主循环保证（先账后铃，绝不 pre-commit 发未落账的铃）。
"""

from __future__ import annotations

from collections.abc import Sequence


class StallCounter:
    """双口径连续计数器：成功清零失败计；换规划清零重复计；数满即报铃。"""

    def __init__(self, n: int = 5, repeat_n: int = 2) -> None:
        if n <= 0 or repeat_n <= 0:
            raise ValueError("失速阈值 n / repeat_n 必须为正整数")
        self._n = n
        self._repeat_n = repeat_n
        self._streak = 0
        self._last_plan = ""
        self._plan_repeat = 0

    def observe(self, *, ok: bool) -> bool:
        """记录一次工具结果：成功清零连败；失败累加，数满 N 返回 True（拉铃）。"""
        self._streak = 0 if ok else self._streak + 1
        return self._streak >= self._n

    def observe_plan(self, updates: Sequence[str]) -> bool:
        """记录本步规划增补：连续 repeat_n 步提出同一规划 → True（拉铃）。

        归一化只做"去空白/丢弃空项/拼键"——不做语义相似度：判断"这两段规划是不是
        一回事"要 LLM，而失速计必须是零成本纯算术（判零归代码，换思路才归 LLMC）。
        """
        key = " | ".join(item.strip() for item in updates if item.strip())
        if not key:
            self._last_plan, self._plan_repeat = "", 0
            return False
        if key == self._last_plan:
            self._plan_repeat += 1
        else:
            self._last_plan, self._plan_repeat = key, 1
        return self._plan_repeat >= self._repeat_n

    def reset(self) -> None:
        """救场重置（W3-3 WF5）：LLMC 提案落地后清零——旧连败/旧重复不算冤枉新打法。

        只清计数不动阈值：阈值是装配期契约，reset 语义仅限"换打法之后重新起算"。
        """
        self._streak = 0
        self._last_plan = ""
        self._plan_repeat = 0


__all__ = ["StallCounter"]
