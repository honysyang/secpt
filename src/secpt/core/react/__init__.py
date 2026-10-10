"""自研 ReAct 循环（最小切片）。

协议：``ACTION: <tool> <args>`` / ``ANSWER: <text>``（文本协议，后续可升级结构化 tool_calls）。
"""

from secpt.core.react.prompts import DEFAULT_SYSTEM_PROMPT
from secpt.core.react.runner import ReactResult, run_react

__all__ = ["DEFAULT_SYSTEM_PROMPT", "ReactResult", "run_react"]
