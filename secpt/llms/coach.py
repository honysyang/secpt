"""失速救场 LLMC（WF5）：读双图给"换思路"提案，只提案不裁决。

纪律（机制硬规，与 Distiller/Reporter 同款分工）：
- **单一 structured 调用**：提案形状由 pydantic 钉死（idea/reason），话术之外
  零权限——Coach 手里没有 dispatcher、没有账本写口、没有预算杠杆；
- **idea 空白 = 无话可说 → None**：提案的可选性由器官层收口，调用方据此走
  降级路径（M2 简化：暂不支持"无话也换路"的兜底）；
- **库层不吞错**：LlmError 原样上抛，降级兜在高一层（loop 的拉铃段）统一做——
  可降级器官的纪律是"降级必须留痕"，而不是"哪里错哪里藏"；
- **不落账**：audit(actor=llmc) 由 loop 在拿到提案那一刻落（先账后吸收），
  Coach 本身保持无状态纯函数式，同一提案重放安全。

R3 风险红线（L678）：M2 事件线仅落铃不做自动接管。救场提案必须经主 LLM 下一包
吸收后才生效——所以 Coach 绝不提供"直接改预算/改计划/替跑"的口，这正是本模块
没有第二个 public 方法的原因。
"""

from __future__ import annotations

from pydantic import BaseModel

from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message


class Proposal(BaseModel):
    """教练提案的固定形状：idea 是"换去哪"，reason 是"为什么旧路走死"。"""

    idea: str
    reason: str


class Coach:
    """LLMC（Coach）：双图+规划进 prompt，一次 structured 调用出提案。

    无状态：每次失速拉铃独立调一次；救场名额（每 run 一次）由主循环管，
    不在本类——名额是 run 级事实，Coach 无权持有。
    """

    def __init__(self, client: LlmClient) -> None:
        """依赖注入统一 chat 协议客户端（与主 LLM/Distiller 共用同一条协议链）。"""
        self._client = client

    def propose(self, graphs: str, plan: str) -> Proposal | None:
        """读双图摘要 + 当前规划，产出一个"换思路"提案；无话可说返回 None。

        graphs/plan 都是已渲染文本（graph_summary / 注意力包的规划段）——LLMC
        与主 LLM 看同一份世界，不用第二套取数路径。
        """
        prompt = self._prompt(graphs, plan)
        proposal = self._client.structured(
            [Message(role="user", content=prompt)], Proposal, temperature=TEMPERATURE_PLAN
        )
        if not proposal.idea.strip():
            return None
        return proposal

    def _prompt(self, graphs: str, plan: str) -> str:
        """给 LLMC 的救场材料：现状（双图）+ 打法（规划）+ 一道单选题。"""
        return "\n".join(
            [
                "你是救场教练：主循环连败拉铃，需要换一个思路继续推进目标。",
                "下面是任务现状与当前打法，请诊断为什么旧路走死，给一个可执行的换法。",
                "只输出 JSON 对象：",
                '{"idea": "一句可直接照做的换法", "reason": "旧路为什么走死"},',
                "若无话可说 idea 留空字符串。",
                "── 双图现状 ──",
                graphs or "（双图暂无内容）",
                "── 当前规划 ──",
                plan or "（暂无规划）",
            ]
        )


__all__ = ["Coach", "Proposal"]
