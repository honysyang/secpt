"""evaluation：AI 评判层（替代 flag 正则判定）。

三层设计：
    ① anticheat.py  确定性反作弊（答案来源核查、本地答案文件直读检测）
    ② jev.py        Jev 类型化高速门控（yes/no + 校准概率，低置信升级）
    ③ judge.py      LLM judge 终裁（rubric + 证据 → 结构化 verdict）

verdict.py 负责编排与落盘（var/jobs/<id>/verdict.json + events.jsonl verdict 事件）。

设计原则（对应需求"flag 格式不固定，AI 自己判定"）：
- 不依赖任何 flag 格式正则；评判对象是"证据是否满足完成条件"
- ground truth（答案/完成态）由出题方提供，AI 判定的是 agent 证据与它的匹配
- agent 自报的 ANSWER 只是证据之一，且必须经过反作弊核查
"""

from secpt.evaluation.verdict import Verdict, evaluate_job

__all__ = ["Verdict", "evaluate_job"]
