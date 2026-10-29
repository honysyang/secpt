"""Orchestrator（主 LLM）的器官 facade（§6.1 前两行 + §2.3 智能层）。

为什么 facade 而不是主循环裸调 client：§6.1 把"主 LLM·一步 / 主 LLM·进场"的
签名钉死为 main_llm.step(pkg) / main_llm.entrance(input)——循环代码不关心
prompt 长什么样、温度用哪档，器官自己管自己的发言纪律（规划档 0~0.2）。

话术来源：prompt 资产库（``secpt/assets/prompts``，可用 ``SECPT_ASSETS_DIR``
覆盖）——本模块只持"用哪个资产 + 填什么值"，不再内联自然语言常量。

无状态：除了底层 client 不持有任何上下文，同一实例可连续 run。
"""

from __future__ import annotations

from secpt.assets import prompt_text, render_prompt
from secpt.llms.client import TEMPERATURE_PLAN, Goal, LlmClient, Message, StepOutput
from secpt.store.attention import AttentionPack


class MainLlm:
    """§6.1 接口面。entrance 走 Goal schema 缺项追问（schema 重试天然承接）；step 走包。"""

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def entrance(self, user_input: str) -> Goal:
        """进场三问固化：{target, objective, success_criteria}，任何一项缺失即重试追问。"""
        messages = [
            Message(role="system", content=prompt_text("main.system")),
            Message(role="user", content=render_prompt("main.entrance", user_input=user_input)),
        ]
        return self._client.structured(messages, Goal, temperature=TEMPERATURE_PLAN)

    def step(self, pkg: AttentionPack) -> StepOutput:
        """主 LLM·一步：消费注意力包，产出结构化 StepOutput（thought/plan/≤4 calls）。"""
        messages = [
            Message(role="system", content=prompt_text("main.system")),
            Message(role="user", content=pkg.render()),
        ]
        return self._client.structured(messages, StepOutput, temperature=TEMPERATURE_PLAN)


__all__ = ["MainLlm"]
