"""任务/对话路由（M5-22）：统一入口的"主智能体自己判断"。

为什么单独一个器官：``secpt run`` 的入参从"--goal 必填"变成了"一句话自然语言"，
需要一次轻量结构化调用判断"这是任务还是咨询"。判断结果直接决定走渗透闭环
（进场三问 + 主循环 + 出场六验）还是走单轮直答（不落账）——这正是需求文档
L2-3 的口径："有 goal → 渗透任务模式；无 goal → 正常对话模式"。

纪律：
- 单一 structured 调用，schema 钉死（mode/goal/reply），没有别的权限；
- 模型若判 pentest 却没给 goal，用原文兜底（用户那句话就是任务描述）；
- 模型若判 chat 却没给答复，落一句诚实的占位说明，不编内容；
- LlmError 原样上抛（由 main 兜底成非零退出）——路由失败不该被吞成"猜一个模式"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from secpt.assets import render_prompt
from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message

_EMPTY_REPLY = "（模型未给出答复）"


class Route(BaseModel):
    """路由结果：mode 决定走哪条路，goal/reply 只有一个会被用到。"""

    mode: Literal["pentest", "chat"]
    goal: str = ""
    reply: str = ""


class Router:
    """一次调用判模式：任务 → goal；咨询 → reply。无状态，可复用。"""

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def decide(self, text: str) -> Route:
        """判断输入走哪条路；返回值保证"该填的字段非空"。"""
        route = self._client.structured(
            [Message(role="user", content=render_prompt("route", user_input=text))],
            Route,
            temperature=TEMPERATURE_PLAN,
        )
        if route.mode == "pentest":
            return route.model_copy(update={"goal": route.goal.strip() or text.strip()})
        return route.model_copy(update={"reply": route.reply.strip() or _EMPTY_REPLY})


__all__ = ["Route", "Router"]
