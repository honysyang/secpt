"""注意力包（§2.3：store/attention.py，纯函数，现打包即弃）。

包内容按 WF2 时序：goal / 最近小票 / 规划概要 / 步号 / 双图摘要（W2-2 已接线，
graph.graph_summary 投影，空图返回空串时不渲染该节）。M1 的三条边界（写进规则注释）：
- 经验不进包：经验库 M2 才激活，三态规则（verified=正典进含命令 / claimed=只当线索
  隐藏命令 / 连续 0 命中=降级不进包）作为注释与模型字段预留，禁止提前实现。
- 打包即弃：包是一步一现的临时物，不落库、不缓存——黑板里只有事实（小票/双图），
  包只是把事实投影给主 LLM 的一次性视图。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from secpt.llms.client import Goal
from secpt.store.ledger import TicketRow


class PackTicket(BaseModel):
    """小票的注意力包投影：只带喂模型的三个字段 + 工具名。"""

    tool_name: str
    exit_code: int | None
    summary: str


class AttentionPack(BaseModel):
    """一步的注意力包：决定主 LLM 本步"看到什么"。render() 产出纯文本进 user 消息。"""

    goal: Goal
    step_no: int
    plan_summary: str
    graph_summary: str = ""  # 空串 = 双图尚无事实，render() 省略该节
    recent_tickets: list[PackTicket] = Field(default_factory=list)
    experience_snapshot: list[str] = Field(default_factory=list)  # M2：三态规则接线位

    def render(self) -> str:
        """渲染纯文本包。字段缺失显式标占位，不给模型留"看到空字符串"的歧义。"""
        lines: list[str] = [
            "## 任务",
            f"目标: {self.goal.target}",
            f"目的: {self.goal.objective}",
            f"成功标准: {self.goal.success_criteria}",
            f"## 当前第 {self.step_no} 步",
            f"## 规划概要\n{self.plan_summary or '（暂无）'}",
        ]
        if self.graph_summary:
            lines.append(f"## 双图摘要\n{self.graph_summary}")
        tickets = (
            "\n".join(
                f"- [{t.tool_name}] exit={t.exit_code}: {t.summary}" for t in self.recent_tickets
            )
            or "- （本任务还没有任何小票）"
        )
        lines.append(f"## 最近小票\n{tickets}")
        if self.experience_snapshot:
            lines.append("## 可用经验\n" + "\n".join(f"- {e}" for e in self.experience_snapshot))
        return "\n".join(lines)


def build(
    goal: Goal,
    step_no: int,
    plan_summary: str,
    recent: list[TicketRow],
    graph_summary: str = "",
) -> AttentionPack:
    """从账本事实装配注意力包（纯函数现打包即弃；M1 经验不进包）。"""
    return AttentionPack(
        goal=goal,
        step_no=step_no,
        plan_summary=plan_summary,
        graph_summary=graph_summary,
        recent_tickets=[
            PackTicket(tool_name=t.tool_name, exit_code=t.exit_code, summary=t.summary)
            for t in recent
        ],
    )


__all__ = ["AttentionPack", "PackTicket", "build"]
