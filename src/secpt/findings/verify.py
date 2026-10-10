"""对抗式自检与复测：让"另一个自己"用不同路径再触发一次。

复用现有基建（不造新轮子）：
- 执行 = run_engine 起一个验证 job（目标/复现步骤来自 finding）
- 裁决 = evaluation.evaluate_job（AI 评判链：反作弊→ground truth→judge）
- 落点 = store.record_reproduction（reproduced 自动升级 confirmed——唯一合法路径）

verify（对抗式自检）：指令要求"用与原始复现**不同的参数/路径**独立再触发"，
防的是单一触发路径上的巧合与环境污染。
retest（复测）：最小化重放原始步骤，回答"这个洞现在还在吗"
（reproduced / fixed / inconclusive 三档）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from secpt.findings.models import Finding
from secpt.findings.store import FindingStore


def _build_objective(f: Finding, *, adversarial: bool) -> str:
    style = (
        "【对抗式复现要求】必须用与下列原始复现**不同的参数值或路径**独立再触发一次"
        "（例如改 payload 编码、换接口入口、换触发顺序），排除单一路径巧合。\n"
        if adversarial
        else "【最小化复测要求】只做验证该漏洞是否仍然存在的必要步骤，不要扩展测试面。\n"
    )
    return f"""【任务：漏洞独立验证】

目标：{f.target}
漏洞：{f.title}（{f.vuln_class or "未分类"}，严重度 {f.severity}）
描述：{f.description}

原始复现步骤：
{f.reproduction}

{style}
如果成功再次触发并拿到证据：输出 ANSWER: REPRODUCED
如果按步骤无法触发（目标已修复/条件不满足）：输出 ANSWER: NOT_REPRODUCED
如果因环境/网络原因无法判断：输出 ANSWER: INCONCLUSIVE
"""


async def _run_verification(
    f: Finding,
    *,
    adversarial: bool,
    llm=None,
    model: str = "deepseek-chat",
    job_id: str | None = None,
) -> dict:
    """起一个验证 job 并用评判层裁决。返回 {verdict, job_id, verdict_detail}。"""
    from secpt.cli.engine_runner import run_engine
    from secpt.evaluation.verdict import evaluate_job

    vjob = (
        job_id or f"{f.id}-{'verify' if adversarial else 'retest'}-{int(__import__('time').time())}"
    )
    objective = _build_objective(f, adversarial=adversarial)

    result = await asyncio.to_thread(
        run_engine,
        objective=objective,
        profile="ctf",
        job_id=vjob,
        llm=llm,
        model_primary=model,
    )
    # 裁决：ANSWER REPRODUCED = ground truth 确定性比对（evaluate_job 的
    # ground_truth_match 路径），无 key/失败则 heuristic 兜底
    import tempfile

    gtf = Path(tempfile.mkdtemp()) / "gt.txt"
    gtf.write_text("REPRODUCED", encoding="utf-8")
    try:
        verdict = await evaluate_job(vjob, ground_truth_file=str(gtf), model=model, llm=llm)
        ok = verdict.complete
    except Exception:
        ok = False
    answer = (result.flag or "").upper()
    if ok or "REPRODUCED" in answer:
        final = "reproduced"
    elif "NOT_REPRODUCED" in answer:
        final = "fixed"
    else:
        final = "inconclusive"
    return {"verdict": final, "job_id": vjob, "answer": result.flag or ""}


async def verify_finding(
    finding_id: str, *, store: FindingStore | None = None, llm=None, model: str = "deepseek-chat"
) -> Finding:
    """对抗式自检：不同路径独立复现 → reproduced 升级 confirmed。"""
    st = store or FindingStore()
    f = st.get(finding_id)
    if f is None:
        raise KeyError(finding_id)
    r = await _run_verification(f, adversarial=True, llm=llm, model=model)
    st.record_reproduction(finding_id, job_id=r["job_id"], verdict=r["verdict"])
    return st.get(finding_id) or f


async def retest_finding(
    finding_id: str, *, store: FindingStore | None = None, llm=None, model: str = "deepseek-chat"
) -> Finding:
    """复测：最小化重放，三档结论（confirmed 的洞 fixed 不自动降级，人工裁决）。"""
    st = store or FindingStore()
    f = st.get(finding_id)
    if f is None:
        raise KeyError(finding_id)
    r = await _run_verification(f, adversarial=False, llm=llm, model=model)
    st.record_reproduction(finding_id, job_id=r["job_id"], verdict=r["verdict"])
    return st.get(finding_id) or f
