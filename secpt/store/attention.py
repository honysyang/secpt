"""注意力包（§2.3：store/attention.py，纯函数，现打包即弃）。

包内容按 WF2 时序：goal / 最近小票 / 规划概要 / 步号 / 双图摘要（W2-2 已接线，
graph.graph_summary 投影，空图返回空串时不渲染该节）/ 经验回放位（W3-1 激活）。

经验三态规则（W3-1 拍板，§63 续记）：
- 经验回放三态（W3-1 激活）：verified=正典进包、命令原样带出；claimed=仅当线索、
  命令藏住；rejected=不进包。fetch_replay 在 SQL 层已排 rejected，build 的投影层
  再挡一次——双闸互为备份，谁独立失效包里也不能出现已死经验。
- 打包即弃：包是一步一现的临时物，不落库、不缓存——黑板里只有事实（小票/双图/经验），
  包只是把事实投影给主 LLM 的一次性视图。
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from secpt.llms.client import Goal
from secpt.store.experience import ExperienceRow
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


def _project_experience(rows: Sequence[ExperienceRow]) -> list[str]:
    """经验条目的三态投影（双闸第二道：rejected 在此再挡一次）。

    - verified：正典进包，命令原样带出（回放即复用）；
    - claimed：仅当线索，命令藏住（裁判未过不许给命令，防半真经验误导）；
    - rejected：不进包（fetch_replay SQL 层已排，这里兜底防独立失效）。
    """
    lines: list[str] = []
    for row in rows:
        if row.verify_state == "rejected":
            continue
        if row.verify_state == "verified":
            rendered = f"[verified] {row.summary}"
            if row.command:
                rendered += f"（命令：{row.command}）"
        else:
            rendered = f"[claimed|仅当线索] {row.summary}"
        lines.append(rendered)
    return lines


def build(
    goal: Goal,
    step_no: int,
    plan_summary: str,
    recent: list[TicketRow],
    graph_summary: str = "",
    experiences: Sequence[ExperienceRow] = (),
) -> AttentionPack:
    """从账本事实装配注意力包（纯函数现打包即弃；经验经三态投影进包）。"""
    return AttentionPack(
        goal=goal,
        step_no=step_no,
        plan_summary=plan_summary,
        graph_summary=graph_summary,
        recent_tickets=[
            PackTicket(tool_name=t.tool_name, exit_code=t.exit_code, summary=t.summary)
            for t in recent
        ],
        experience_snapshot=_project_experience(experiences),
    )


__all__ = ["AttentionPack", "PackTicket", "build"]
