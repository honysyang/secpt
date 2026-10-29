"""任务/对话路由（M5-22）：统一入口的"主智能体自己判断"。

为什么单独一个器官且**只做分类不做回答**：``secpt run`` 的入参从"--goal 必填"
变成了"一句话自然语言"，需要一次轻量结构化调用判断"这是任务还是咨询"。
判 chat 时**不在这里作答**——回答统一交给带身份系统提示的对话器官
（``chat.system``），否则同一条咨询从 ``secpt run`` 和 ``secpt chat`` 进来会
得到两种人格的答案（M5-23 实测踩到过）。

纪律：
- 单一 structured 调用，schema 钉死（mode/goal），没有别的权限；
- 模型若判 pentest 却没给 goal，用原文兜底（用户那句话就是任务描述）；
- LlmError 原样上抛（由 main 兜底成非零退出）——路由失败不该被吞成"猜一个模式"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from secpt.assets import render_prompt
from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message


class Route(BaseModel):
    """路由结果：mode 决定走哪条路，goal 只在 pentest 时使用。"""

    mode: Literal["pentest", "chat"]
    goal: str = ""


class Router:
    """一次调用判模式：任务 → goal；咨询 → chat（由调用方交给对话器官作答）。"""

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def decide(self, text: str) -> Route:
        """判断输入走哪条路；判 pentest 且 goal 为空时用原文兜底。"""
        route = self._client.structured(
            [Message(role="user", content=render_prompt("route", user_input=text))],
            Route,
            temperature=TEMPERATURE_PLAN,
        )
        if route.mode == "pentest":
            return route.model_copy(update={"goal": route.goal.strip() or text.strip()})
        return route


__all__ = ["Route", "Router"]
