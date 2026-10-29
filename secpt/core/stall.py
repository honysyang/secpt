"""失速计（§9 双引擎对表：连续 N 步无实质进展 → 拉铃）。

三个计数口径（都是"无实质进展"的早期信号）：
- **失败计**（M1）：本步工具回执是否 exit_code==0，连续失败的步数满 N 拉铃；
- **重复规划计**（M5，设计文档 L84 "重复抑制"）：同一段非空 plan_updates
  连续提出满 repeat_n 次拉铃——同输入相似输出是"原地打转"的早期信号，
  比等 5 次工具失败更早。空 updates 不算重复（模型没提规划 ≠ 重复提）。
- **重复调用计**（M5-31）：同一 (工具, 参数) 的调用累计满 repeat_calls 次拉铃。
  真实 run 实测两种既有口径对此全盲：工具回执 exit=0（失败计清零）、规划
  每步换个说法（重复规划计不触发），模型却把同一个 crt.sh URL 抓了 8 次、
  同一对文件读了 10+ 步——"同输入"的判据在调用层，不在规划层。

失败-拉铃-出口的顺序契约：拉铃只报告 True 不落账——落账（小票/水位线）与
E_STALL 异常出口的顺序由主循环保证（先账后铃，绝不 pre-commit 发未落账的铃）。
"""

from __future__ import annotations

from collections.abc import Sequence


class StallCounter:
    """三口径计数器：成功清零失败计；换规划清零重复计；同参数调用累计到顶拉铃。"""

    def __init__(self, n: int = 5, repeat_n: int = 2, repeat_calls: int = 4) -> None:
        if n <= 0 or repeat_n <= 0 or repeat_calls <= 0:
            raise ValueError(
                f"失速阈值 n / repeat_n / repeat_calls 必须为正整数，"
                f"got {n} / {repeat_n} / {repeat_calls}"
            )
        self._n = n
        self._repeat_n = repeat_n
        self._repeat_calls = repeat_calls
        self._streak = 0
        self._last_plan = ""
        self._plan_repeat = 0
        self._call_counts: dict[str, int] = {}

    def observe(self, *, ok: bool) -> bool:
        """记录一次工具结果：成功清零连败；失败累加，数满 N 返回 True（拉铃）。"""
        self._streak = 0 if ok else self._streak + 1
        return self._streak >= self._n

    def observe_calls(self, keys: Sequence[str]) -> bool:
        """记录本步的调用签名（M5-31）：同签名累计满 repeat_calls 次拉铃。

        签名 = 工具名 + 规范化参数（调用方拼好），同一 URL 反复 GET、同一文件
        反复读、同参数反复扫描都会撞线。计数是**累计**而非连续：中间插一步别的
        动作不洗白"已经重复过 3 次"的事实。恰好到顶那次返回 True（此后不再重复
        拉铃，等 reset 或出口）；reset（救场换打法）后计数清零重新起算。
        """
        ring = False
        for key in keys:
            if not key:
                continue
            count = self._call_counts.get(key, 0) + 1
            self._call_counts[key] = count
            if count == self._repeat_calls:
                ring = True
        return ring

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
        三个口径一起清：救场给的是新打法，旧的同参数重复也不该再记在新打法头上。
        """
        self._streak = 0
        self._last_plan = ""
        self._plan_repeat = 0
        self._call_counts.clear()


__all__ = ["StallCounter"]
