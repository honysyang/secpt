"""report_finding：结构化漏洞上报工具（Engine extra_tool 装配）。

确认纪律在工具契约里：
- 创建即 inferred——工具没有 confirmed 入口，升级只能走对抗式复现
- evidence 自动捕获调用点的上下文（当前 job/step + 可选原文引用），
  B3 流量层接入后 kind=traffic 的快照由 traffic 钩子补挂
- 强制要求 reproduction（最小复现步骤）——没有复现路径的发现不可验证，
  等于没有
"""

from __future__ import annotations

import time

from secpt.findings.models import EvidenceRef, Finding
from secpt.findings.store import FindingStore

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "一句话标题（含影响，如：XSS 可窃取管理员会话）",
        },
        "vuln_class": {
            "type": "string",
            "description": "漏洞类别，如 saml_xsw / sql_injection / auth_bypass",
        },
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "confidence": {"type": "number", "description": "你的把握 0-1；低于 0.7 请说明不确定点"},
        "target": {"type": "string", "description": "目标 host/url（verify/retest 的输入，必填）"},
        "description": {"type": "string", "description": "漏洞描述：根因+触发条件"},
        "reproduction": {
            "type": "string",
            "description": "最小复现步骤（命令/请求），verify 会按此独立重放",
        },
        "impact": {"type": "string"},
        "evidence_quote": {"type": "string", "description": "支撑证据原文（响应片段），可为空"},
    },
    "required": ["title", "target", "description", "reproduction"],
}

TOOL_DOC = (
    "- report_finding(title, vuln_class, severity, confidence, target, description,\n"
    "  reproduction, impact, evidence_quote)  上报一个漏洞发现。\n"
    "  纪律：只有本次真实触发并拿到响应证据才可上报（confidence≥0.7）；\n"
    "  疑似/未触发请先用 bash 自行验证。上报即 inferred（疑似），\n"
    "  系统会安排独立路径复现，复现成功才升级为 confirmed。"
)


def make_report_finding_fn(store, *, job_id: str, current_step) -> object:
    """生成 report_finding 的 fn(args)->str。current_step: ()->int 取当前步号。"""

    async def fn(args: dict) -> str:
        args = args or {}
        missing = [
            k
            for k in ("title", "target", "description", "reproduction")
            if not str(args.get(k) or "").strip()
        ]
        if missing:
            return (
                f"Error: 缺少必填字段 {missing}（target/reproduction 必填：不可验证的发现等于没有）"
            )
        severity = str(args.get("severity") or "medium")
        if severity not in ("critical", "high", "medium", "low"):
            return f"Error: severity 必须是 critical/high/medium/low，收到 {severity!r}"
        try:
            confidence = float(args.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5

        evidence = [EvidenceRef(kind="step", ref=f"{job_id}:{current_step()}", role="proof")]
        quote = str(args.get("evidence_quote") or "").strip()
        if quote:
            evidence.append(EvidenceRef(kind="quote", ref=quote[:800], role="supporting"))

        f = Finding(
            title=str(args["title"])[:200],
            vuln_class=str(args.get("vuln_class") or "")[:60],
            severity=severity,  # type: ignore[arg-type]
            confidence=max(0.0, min(1.0, confidence)),
            target=str(args["target"])[:300],
            description=str(args["description"])[:2000],
            reproduction=str(args["reproduction"])[:2000],
            impact=str(args.get("impact") or "")[:1000],
            evidence=evidence,
            first_seen={"job_id": job_id, "step": current_step(), "ts": time.time()},
            job_id=job_id,
        )
        # 同 target+title 去重（重复上报合并而不是刷屏）
        for old in store.list(target=f.target, limit=200):
            if old.title == f.title and old.status not in ("false_positive", "ignored"):
                return (
                    f"OK(duplicate): 已有同类发现 {old.id}（status={old.status}），"
                    f"补充证据请用其它方式说明"
                )
        store.save(f)
        return (
            f"OK: 已登记 finding {f.id}（status=inferred）。"
            f"只有独立路径复现成功才会升级为 confirmed。"
        )

    return fn


def engine_tool(schema_extra: dict | None = None) -> dict:
    """OpenAI function schema（engine 的 extra_tools 格式）。"""
    return {
        "type": "function",
        "function": {
            "name": "report_finding",
            "description": "上报一个真实触发过的漏洞发现（创建即 inferred，经对抗式复现才升级 confirmed）",
            "parameters": SCHEMA,
        },
    }


def maybe_attach(extra_tools: dict, names: list[str], job_id: str) -> tuple[dict, dict]:
    """names 声明了 report_finding 则装配之。返回 (extra_tools, step_box)；
    step_box 由 StepTracker 在 engine 每步更新，供证据的 step 指针取值。"""
    box = {"n": 0}
    if "report_finding" not in names:
        return extra_tools, box
    tools = dict(extra_tools)
    tools["report_finding"] = (
        engine_tool(),
        make_report_finding_fn(FindingStore(), job_id=job_id, current_step=lambda: box["n"]),
    )
    return tools, box


class StepTracker:
    """hooks 包装：记录当前步号后原样委托（__getattr__ 透传）。"""

    def __init__(self, inner, box: dict) -> None:
        self._inner = inner
        self._box = box

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def on_step(self, steps: int, elapsed: float) -> None:
        self._box["n"] = steps
        self._inner.on_step(steps, elapsed)
