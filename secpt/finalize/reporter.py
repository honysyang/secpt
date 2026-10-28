"""LLMA 终场/半场报告（WF9）：结构化 Report + Markdown 段。

纪律（机制硬规，非文档约定）：
- verified-only：findings 由纯代码从账本取（verify_state='verified'），LLM
  结构上进不了 findings 列表，不可能发明 claims；
- 锚点校验：evidence_ref 空白的 verified 行剔除出报告 + audit 留痕（actor=llma）；
- LLM 只写 summary：schema ``{"summary": str}``（规划档温度），不吞 LlmError——
  main.py 兜底降级，绝不在库层悄悄吞掉。
- failing_counts 语义：final normal 收束以机制为准计 0（能落 normal 说明出口
  三验当场已过）；异常/半场报告按当下账本重验如实上数（ WF6/异常出口双方数字，
  半场前的中途失败只能在这里反映出来）。
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from secpt.core.loop import ExitVerdict
from secpt.finalize.exit_checklist import run_checklist
from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message
from secpt.store import ledger


class ReportFinding(BaseModel):
    """报告条目：verified finding 的三字段投影（锚点必在，否则进不了这里）。"""

    title: str
    detail: str
    evidence_ref: str


class Report(BaseModel):
    """WF9 结构化输出（M1 形状）：半场报告同 schema，多 half_court 标记。"""

    task_id: str
    exit_kind: str
    half_court: bool
    summary: str
    findings: list[ReportFinding] = Field(default_factory=list)
    failing_counts: dict[str, int] = Field(default_factory=dict)


class _Summary(BaseModel):
    """LLM 结构化调用的唯一产出框：LLMA 只转述不转写 verdict，规则见模块 docstring。"""

    summary: str


def render_markdown(rep: Report) -> str:
    """报告 Markdown 段：标题带终场/半场字样，条目逐条带锚点，LLM 无插手机会。"""
    title = "半场报告" if rep.half_court else "终场报告"
    lines = [
        f"# {title}（task {rep.task_id}）",
        "",
        f"exit_kind={rep.exit_kind}  half_court={rep.half_court}",
        f"failing_counts={rep.failing_counts}",
        "",
        "## confirmed findings（verified-only，逐条锚点）",
    ]
    if rep.findings:
        for f in rep.findings:
            lines.append(f"- {f.title}：{f.detail}（锚点：{f.evidence_ref}）")
    else:
        lines.append("- （无 verified findings，如实照录）")
    lines += ["", "## 摘要", "", rep.summary]
    return "\n".join(lines)


class Reporter:
    """LLMA（Reporter）：WF9 的 M1 实现——账本取材定 findings，LLM 只写摘要。"""

    def __init__(self, client: LlmClient) -> None:
        """依赖注入统一 chat 协议客户端（KISS：M1 只用一次 structured 调用）。"""
        self._client = client

    def generate(
        self, conn: sqlite3.Connection, task_id: str, *, half_court: bool = False
    ) -> Report:
        """终场/半场报告：账本取材 → 剔除无锚行 → 出场/异常双方计数 → LLM 摘要。

        库层不吞错：LlmError（脚本耗尽/超时）原样上抛给 main.py 兜底降级。
        """
        task = ledger.get_task(conn, task_id)
        exit_kind = task.exit_kind or ""

        verified = ledger.task_findings(conn, task_id, verify_state="verified")
        dropped = [f for f in verified if not f.evidence_ref.strip()]
        if dropped:
            ledger.audit(
                conn,
                task_id,
                "llma",
                f"报告剔除 {len(dropped)} 条无锚 claims：" + "、".join(f.title for f in dropped),
            )
        kept = [f for f in verified if f.evidence_ref.strip()]
        findings = [
            ReportFinding(title=f.title, detail=f.detail, evidence_ref=f.evidence_ref) for f in kept
        ]

        # failing_counts：normal 以机制为准计 0；异常/半场按当下账本重验（模块 docstring）。
        abnormal_count = 0 if exit_kind == "normal" else 1
        checklist_failures: list[str] = []
        if exit_kind != "normal":
            verdict: ExitVerdict = run_checklist(conn, task_id)
            checklist_failures = verdict.failures

        summary = self._client.structured(
            [Message(role="user", content=self._context(findings, checklist_failures, half_court))],
            _Summary,
            temperature=TEMPERATURE_PLAN,
        ).summary

        ledger.audit(
            conn,
            task_id,
            "llma",
            f"{'半场' if half_court else '终场'}报告落账"
            f"（verified findings {len(findings)} 条，无锚 {len(dropped)} 条未入）",
        )
        return Report(
            task_id=task_id,
            exit_kind=exit_kind,
            half_court=half_court,
            summary=summary,
            findings=findings,
            failing_counts={"exit_checklist": len(checklist_failures), "abnormal": abnormal_count},
        )

    def _context(self, findings: list[ReportFinding], failures: list[str], half_court: bool) -> str:
        """喂给 LLM 的转述材料：只有 verified 条目与计数，claims 无中生有的路被真正堵死。"""
        kind = "半场（异常出口）" if half_court else "终场"
        lines = [
            f"任务{kind}报告摘要（只转述，不发明新发现）。",
            f"verified 条目 {len(findings)} 条（逐条含证据锚点）：",
        ]
        lines += [f"- {f.title}：{f.detail}；锚点 {f.evidence_ref}" for f in findings]
        lines.append(
            f"出场/异常计数：exit_checklist 失败 {len(failures)} 条"
            + (f"：{'；'.join(failures)}" if failures else "")
        )
        return "\n".join(lines)
