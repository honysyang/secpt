"""失速救场 / 规划重估 LLMC（WF5）：读双图给"换思路"提案，只提案不裁决。

纪律（机制硬规，与 Distiller/Reporter 同款分工）：
- **单一 structured 调用**：提案形状由 pydantic 钉死（idea/reason），话术之外
  零权限——Coach 手里没有 dispatcher、没有账本写口、没有预算杠杆；
- **idea 空白 = 无话可说 → None**：提案的可选性由器官层收口，调用方据此走
  降级路径（M2 简化：暂不支持"无话也换路"的兜底）；
- **库层不吞错**：LlmError 原样上抛，降级兜在高一层（loop 的拉铃段）统一做——
  可降级器官的纪律是"降级必须留痕"，而不是"哪里错哪里藏"；
- **不落账**：audit(actor=llmc) 由 loop 在拿到提案那一刻落（先账后吸收），
  Coach 本身保持无状态纯函数式，同一提案重放安全。

两个入口共用同一提案形状与同一条纪律，差别只在"为什么现在重估"：
- :meth:`propose` —— 失速拉铃（连败 N 步）后的换思路；
- :meth:`review` —— 双图变化事件线在步界触发的规划重估（M5）。

R3 风险红线（L678）：事件线只落铃不自动接管。救场/重估提案必须经主 LLM 下一包
吸收后才生效——所以 Coach 绝不提供"直接改预算/改计划/替跑"的口，这正是本模块
只有两个 public 方法的原因。
"""

from __future__ import annotations

from pydantic import BaseModel

from secpt.assets import render_prompt
from secpt.llms.client import TEMPERATURE_FREESTYLE, TEMPERATURE_PLAN, LlmClient, Message


class Proposal(BaseModel):
    """教练提案的固定形状：idea 是"换去哪"，reason 是"为什么旧路走死"。"""

    idea: str
    reason: str


_EMPTY_GRAPHS = "（双图暂无内容）"
_EMPTY_PLAN = "（暂无规划）"


class Coach:
    """LLMC（Coach）：双图+规划进 prompt，一次 structured 调用出提案。

    无状态：每次拉铃/重估独立调一次；救场名额（每 run 一次）与重估名额由主
    循环管，不在本类——名额是 run 级事实，Coach 无权持有。
    """

    def __init__(self, client: LlmClient) -> None:
        """依赖注入统一 chat 协议客户端（与主 LLM/Distiller 共用同一条协议链）。"""
        self._client = client

    def propose(self, graphs: str, plan: str) -> Proposal | None:
        """读双图摘要 + 当前规划，产出一个"换思路"提案；无话可说返回 None。

        graphs/plan 都是已渲染文本（graph_summary / 注意力包的规划段）——LLMC
        与主 LLM 看同一份世界，不用第二套取数路径。

        温度取**发散档**（0.8，设计文档 L83"CTF 撞墙场景才升温"）：走到失速救场
        说明按原打法已经走死，此时需要的是跳出局部最优的新思路，而不是在低温下
        复述同一条路。
        """
        return self._ask("coach.propose", graphs, plan, temperature=TEMPERATURE_FREESTYLE)

    def review(self, graphs: str, plan: str) -> Proposal | None:
        """双图变化事件驱动的规划重估（M5）：现有规划是否仍然成立。

        与 propose 的区别是触发语境（事件线 vs 失速铃）与温度（重估是"核对现状"，
        用规划档低温即可；只有撞墙换路才升温），权限完全相同——只提案不裁决，
        无话可说返回 None。
        """
        return self._ask("coach.review", graphs, plan, temperature=TEMPERATURE_PLAN)

    def _ask(self, asset_id: str, graphs: str, plan: str, *, temperature: float) -> Proposal | None:
        """单次结构化调用；两入口共用形状与空白收口，温度由调用方选档。"""
        prompt = render_prompt(
            asset_id,
            graphs=graphs or _EMPTY_GRAPHS,
            plan=plan or _EMPTY_PLAN,
        )
        proposal = self._client.structured(
            [Message(role="user", content=prompt)], Proposal, temperature=temperature
        )
        if not proposal.idea.strip():
            return None
        return proposal


__all__ = ["Coach", "Proposal"]
