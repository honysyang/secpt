"""Orchestrator（主 LLM）的器官 facade（§6.1 前两行 + §2.3 智能层）。

为什么 facade 而不是主循环裸调 client：§6.1 把"主 LLM·一步 / 主 LLM·进场"的
签名钉死为 main_llm.step(pkg) / main_llm.entrance(input)——循环代码不关心
prompt 长什么样、温度用哪档，器官自己管自己的发言纪律（规划档 0~0.2）。

无状态：除了底层 client 不持有任何上下文，同一实例可连续 run。
"""

from __future__ import annotations

from secpt.llms.client import TEMPERATURE_PLAN, Goal, LlmClient, Message, StepOutput
from secpt.store.attention import AttentionPack

_SYS_PROMPT = (
    "你是 Orchestrator——渗透测试任务的编排者。你每一步只能输出一个 JSON 对象："
    '{"thought": 一句理由, "plan_updates": [规划增补...], '
    '"tool_calls": [{"name": 工具名, "args": {参数: 值}}] (≤4), "done": 是否收工}。'
    "不要输出 JSON 以外的任何文本。"
)


class MainLlm:
    """§6.1 接口面。entrance 走 Goal schema 缺项追问（schema 重试天然承接）；step 走包。"""

    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def entrance(self, user_input: str) -> Goal:
        """进场三问固化：{target, objective, success_criteria}，任何一项缺失即重试追问。"""
        messages = [
            Message(role="system", content=_SYS_PROMPT),
            Message(
                role="user",
                content=(
                    "负责进场：把下面的用户输入固化成三问 JSON。"
                    f'{{"target": 谁被测, "objective": 测什么, "success_criteria": 何为成功}}'
                    f"\n\n用户输入：{user_input}"
                ),
            ),
        ]
        return self._client.structured(messages, Goal, temperature=TEMPERATURE_PLAN)

    def step(self, pkg: AttentionPack) -> StepOutput:
        """主 LLM·一步：消费注意力包，产出结构化 StepOutput（thought/plan/≤4 calls）。"""
        messages = [
            Message(role="system", content=_SYS_PROMPT),
            Message(role="user", content=pkg.render()),
        ]
        return self._client.structured(messages, StepOutput, temperature=TEMPERATURE_PLAN)


__all__ = ["MainLlm"]
