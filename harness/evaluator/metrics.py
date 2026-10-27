"""评估器：对执行结果打分和反馈。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EvalResult:
    """评估结果。"""

    score: float  # 0-10
    passed: bool
    feedback: str


class Evaluator:
    """结果评估器。"""

    def __init__(self, model: object) -> None:
        self.model = model

    async def evaluate(self, output: str, criteria: str) -> EvalResult:
        """评估输出是否满足标准。

        生产实现：调用 LLM 评分。
        """
        # TODO: 接入 LLM 评估
        return EvalResult(score=7.0, passed=True, feedback="placeholder")
