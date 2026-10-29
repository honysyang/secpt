"""经验蒸馏器 LLMB（WF10）：run 收束后长期记忆的唯一写入器官。

纪律（机制硬规，非文档约定）：
- **verify_state 由代码 derive**：finding 逐条取账本状态、成功小票一律 claimed、
  失败小票不进候选——LLM 结构上进不了判态位，与 Reporter"转述不转写"同款双保险；
- **锚点是事实不是话**：raw_ref 直接取自账本行（finding.evidence_ref /
  ticket.raw_ref），LLM 只产 summary/command；空锚条目由 store 官门拒落（丢弃审计）；
- **无候选不烧 LLM**：0 findings 且 0 成功小票时直接收束 0 条——mock 演示路径
  离线可跑（audit 留"蒸馏收束 0 条"），演示脚本长度与蒸馏不耦合；
- **库层不吞错**：LlmError 原样上抛，主循环的蒸馏 hook（core/loop._distill）负责
  降级——learning 可降级，但降级必须留 warn。
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from secpt.assets import render_prompt
from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message
from secpt.store import experience, ledger


class _Draft(BaseModel):
    """LLM 只能填这三个字段：idx 指候选序位，判态与锚点都由代码回填。"""

    idx: int
    summary: str
    command: str | None = None


class _DraftList(BaseModel):
    """一次结构化调用的全部产出：条数由候选规模上界约束在 prompt 里。"""

    entries: list[_Draft] = Field(default_factory=list)


class Distiller:
    """LLMB（Distiller）：账本取材定候选，LLM 只蒸馏话术，写 Always 过 store 官门。"""

    def __init__(self, client: LlmClient) -> None:
        """依赖注入统一 chat 协议客户端（同 Reporter 手式，M2 只用一次 structured）。"""
        self._client = client

    def distill(
        self, conn: sqlite3.Connection, task_id: str, environment: str
    ) -> list[experience.ExperienceRow]:
        """收尾蒸馏：候选 = 全 findings + 成功小票 → LLM 选序位写摘要 → store 落账。

        复跑幂等边界（M2 最简）：resume 续跑同 task 再收官会二次蒸馏、可能产生
        重复条目——回放 top-k 只取最新若干条，重复不致误导，去重留给 M3 判票。
        """
        findings = ledger.task_findings(conn, task_id)
        ok_tickets = [
            t for t in ledger.recent_tickets(conn, task_id, limit=100) if t.exit_code == 0
        ]
        # 候选序位表：(verify_state[代码 derive]，raw_ref[事实锚点]，凭据[侦察参考])
        candidates: list[tuple[str, str, str]] = []
        for fnd in findings:
            candidates.append((fnd.verify_state, fnd.evidence_ref, fnd.detail))
        for ticket in ok_tickets:
            candidates.append(("claimed", ticket.raw_ref, ticket.args_json))
        if not candidates:
            ledger.audit(conn, task_id, "llmb", "经验蒸馏收束：无蒸馏候选，0 条落账")
            return []

        prompt = self._prompt(candidates)
        drafts = self._client.structured(
            [Message(role="user", content=prompt)], _DraftList, temperature=TEMPERATURE_PLAN
        )
        recorded: list[experience.ExperienceRow] = []
        skipped = 0
        for d in drafts.entries:
            if not 0 <= d.idx < len(candidates):
                skipped += 1
                continue
            verify_state, raw_ref, _ = candidates[d.idx]
            row = experience.record_experience(
                conn, task_id, environment, verify_state, d.summary, d.command, raw_ref
            )
            if row is not None:
                recorded.append(row)
        ledger.audit(
            conn,
            task_id,
            "llmb",
            f"经验蒸馏完成：落账 {len(recorded)} 条（草案 {len(drafts.entries)} 条，"
            f"越界跳过 {skipped} 条），候选 {len(candidates)} 条",
        )
        return recorded

    def _prompt(self, candidates: list[tuple[str, str, str]]) -> str:
        """喂给 LLMB 的蒸馏材料：候选逐条带判态与锚点，LLM 只负责话术。

        话术本体在 prompt 资产（``distiller``）里，本方法只负责把候选渲染成
        一段清单——资产与取数分离，改话术不必改代码。
        """
        lines = [
            f"- idx={idx} 判态={verify_state} 锚点={raw_ref} 凭据={detail}"
            for idx, (verify_state, raw_ref, detail) in enumerate(candidates)
        ]
        return render_prompt("distiller", candidates="\n".join(lines))


__all__ = ["Distiller"]
