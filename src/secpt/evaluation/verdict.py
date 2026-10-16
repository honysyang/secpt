"""评判编排：证据 → 反作弊 → Jev 门控 → LLM 终裁 → 落盘。

判定链（任一步提前出结果即短路）：
    anticheat.hard_fail          → complete=false, method="anticheat"（确定性零分）
    Jev 可用且高置信            → 采信, method="jev"
    LLM judge 可用且输出可解析   → method="llm_judge"
    其余（假 LLM/网络故障）      → heuristic 兜底, method="heuristic"（低置信，需人工/真实 judge 复核）

产出：
- var/jobs/<job_id>/verdict.json
- events.jsonl 追加 verdict 事件
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from secpt.evaluation.anticheat import check as anticheat_check
from secpt.evaluation.evidence import collect_evidence
from secpt.evaluation.jev import JevGate
from secpt.evaluation.judge import JudgeUnavailable, LLMJudge
from secpt.evaluation.rubric import Rubric, build_rubric, load_rubric_file


@dataclass
class Verdict:
    job_id: str
    complete: bool
    confidence: float  # 0-1
    method: str  # anticheat | jev | llm_judge | heuristic
    satisfied_conditions: list[str] = field(default_factory=list)
    evidence_quote: str = ""
    reasoning: str = ""
    anticheat: dict = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "complete": self.complete,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "satisfied_conditions": self.satisfied_conditions,
            "evidence_quote": self.evidence_quote,
            "reasoning": self.reasoning,
            "anticheat": self.anticheat,
            "created_at": self.created_at or time.time(),
        }


def _default_llm():
    if os.environ.get("SECPT_FAKE_LLM"):
        from secpt.core.job.fakellm import FakeLLM

        return FakeLLM(n_steps=1, job_id="judge")
    from secpt.runtime.llm.client import call_llm

    return call_llm


async def evaluate_job(
    job_id: str,
    *,
    jobs_dir: Path | None = None,
    rubric_file: str | None = None,
    ground_truth_file: str | None = None,
    llm=None,
    model: str = "deepseek-chat",
    jev: JevGate | None = None,
) -> Verdict:
    """评判一个已完成的 job，返回 Verdict 并落盘。

    全链路兜底：评判自身任何未预期异常 → heuristic 降级 verdict，绝不向上抛
    （评判是观测层，不能反过来打断主流程）。
    """
    from secpt.core.job.resume import jobs_dir as _default_jobs_dir

    jd = Path(jobs_dir) if jobs_dir else _default_jobs_dir()
    job_dir = jd / job_id
    if not (job_dir / "events.jsonl").is_file():
        raise FileNotFoundError(f"job {job_id} 无 events.jsonl: {job_dir}")

    try:
        return await _evaluate(
            job_id,
            jd,
            job_dir,
            rubric_file=rubric_file,
            ground_truth_file=ground_truth_file,
            llm=llm,
            model=model,
            jev=jev,
        )
    except FileNotFoundError:
        raise
    except Exception as e:
        print(f"[verdict] 评判链异常，heuristic 兜底: {e}")
        verdict = Verdict(
            job_id=job_id,
            complete=False,
            confidence=0.1,
            method="heuristic",
            reasoning=f"评判链异常（{e}），结果不可信，需人工复核",
        )
        _persist(verdict, job_dir)
        _register_heuristic_review(verdict)
        return verdict


async def _evaluate(
    job_id: str,
    jd: Path,
    job_dir: Path,
    *,
    rubric_file: str | None = None,
    ground_truth_file: str | None = None,
    llm=None,
    model: str = "deepseek-chat",
    jev: JevGate | None = None,
) -> Verdict:
    evidence = collect_evidence(job_id, jd)
    ac = anticheat_check(evidence, job_dir)

    ground_truth = None
    if ground_truth_file:
        ground_truth = Path(ground_truth_file).read_text(encoding="utf-8").strip() or None

    if rubric_file:
        rubric: Rubric = load_rubric_file(rubric_file)
        if ground_truth and not rubric.ground_truth:
            rubric.ground_truth = ground_truth
    else:
        # 由题面重建 objective：优先 task.yaml 快照
        objective = job_id
        task_yaml = job_dir / "task.yaml"
        if task_yaml.is_file():
            try:
                import yaml

                objective = str(
                    yaml.safe_load(task_yaml.read_text(encoding="utf-8"))["spec"]["objective"]
                )
            except Exception:
                pass
        rubric = await build_rubric(objective, llm=None, ground_truth=ground_truth)

    # ① 硬作弊：确定性零分
    if ac.hard_fail:
        verdict = Verdict(
            job_id=job_id,
            complete=False,
            confidence=1.0,
            method="anticheat",
            reasoning="；".join(ac.suspicious),
            anticheat=ac.to_dict(),
        )
        _persist(verdict, job_dir)
        return verdict

    # ①.5 确定性答案比对（非正则）：自报答案与出题方 ground truth 精确/包含匹配
    # 幻觉出与 ground truth 完全一致的答案概率可忽略，故高置信采信
    if evidence.answer and rubric.ground_truth:
        ans, gt = evidence.answer.strip(), rubric.ground_truth.strip()
        if ans and gt and (ans == gt or ans in gt or gt in ans):
            verdict = Verdict(
                job_id=job_id,
                complete=True,
                confidence=0.95,
                method="ground_truth_match",
                reasoning="自报答案与出题方 ground truth 一致（确定性比对）",
                evidence_quote=evidence.answer[:200],
                anticheat=ac.to_dict(),
            )
            _persist(verdict, job_dir)
            return verdict

    # ② Jev 高速门控
    gate = jev if jev is not None else JevGate()
    ctx = (
        f"任务：{rubric.objective_summary}\n"
        f"完成条件：{'；'.join(rubric.completion_conditions)}\n"
        f"agent 自报答案：{evidence.answer or '（无）'}\n"
        f"答案出现在工具输出中：{evidence.answer_in_outputs}\n"
        f"轨迹末尾：\n{evidence.transcript_excerpt(1500)}"
    )
    decision = await gate.decide("根据给定轨迹证据，该任务是否已被真正完成？", ctx)
    if gate.accept(decision):
        verdict = Verdict(
            job_id=job_id,
            complete=decision.answer,
            confidence=decision.probability,
            method="jev",
            reasoning=f"Jev typed decision（{decision.raw_model or 'system-one'}）高置信采信",
            anticheat=ac.to_dict(),
        )
        _persist(verdict, job_dir)
        return verdict

    # ③ LLM judge 终裁
    if llm is None:
        llm = _default_llm()
    try:
        jv = await LLMJudge(llm, model=model).judge(rubric, evidence, ac)
        verdict = Verdict(
            job_id=job_id,
            complete=jv.complete,
            confidence=jv.confidence,
            method="llm_judge",
            satisfied_conditions=jv.satisfied_conditions,
            evidence_quote=jv.evidence_quote,
            reasoning=jv.reasoning,
            anticheat=ac.to_dict(),
        )
    except JudgeUnavailable as e:
        # ④ heuristic 兜底：有自报答案且无明显作弊信号 → 低置信通过
        heuristic_complete = bool(evidence.answer) and not ac.local_answer_file_read
        verdict = Verdict(
            job_id=job_id,
            complete=heuristic_complete,
            confidence=0.3,
            method="heuristic",
            reasoning=f"LLM judge 不可用（{e}），启发式兜底："
            f"自报答案={'有' if evidence.answer else '无'}，"
            f"答案在工具输出中={evidence.answer_in_outputs}；需真实 judge 复核",
            anticheat=ac.to_dict(),
        )
        if evidence.answer and rubric.ground_truth:
            # 有 ground truth 但确定性比对未命中 → 判不完整（置信中等）
            verdict.complete = False
            verdict.confidence = 0.6
            verdict.reasoning += "；自报答案与 ground truth 不符"
    _persist(verdict, job_dir)
    if verdict.method == "heuristic":
        _register_heuristic_review(verdict)
    return verdict


def _register_heuristic_review(verdict: Verdict) -> None:
    """heuristic 兜底 verdict（低置信，未经真实 judge）登记 intercept pending 人工复核队列。

    幂等：同一 job 已有未结算复核记录则跳过。评判是观测层——登记失败绝不影响 verdict。
    """
    try:
        from secpt.governance.intercept import Interceptor

        icp = Interceptor()
        with icp._conn() as c:
            n = c.execute(
                "SELECT COUNT(*) n FROM intercept_pending"
                " WHERE job_id=? AND rule_name=? AND status='pending'",
                (verdict.job_id, "评判复核-heuristic"),
            ).fetchone()["n"]
        if int(n) > 0:
            return
        pid = f"icp-verdict-{uuid.uuid4().hex[:8]}"
        preview = (
            f"job={verdict.job_id} complete={verdict.complete}"
            f" conf={verdict.confidence} method={verdict.method}"
            f" reason={verdict.reasoning[:120]}"
        )
        with icp._conn() as c:
            c.execute(
                """INSERT INTO intercept_pending
                   (id,job_id,target,preview,rule_id,rule_name,status,ts)
                   VALUES (?,?,?,?,?,?, 'pending', ?)""",
                (
                    pid,
                    verdict.job_id,
                    "verdict_review",
                    preview[:200],
                    -1,
                    "评判复核-heuristic",
                    time.time(),
                ),
            )
        print(
            f"[verdict] ⏸ heuristic 兜底已登记人工复核 #{pid}：{preview[:120]}"
            f"（复核后 secpt approve {pid}）"
        )
        with contextlib.suppress(Exception):
            from secpt.runtime.notify import notify

            notify(
                "approval_pending",
                f"secpt 评判待复核 #{pid}（heuristic 兜底）",
                f"{preview[:200]}\n人工复核后：secpt approve {pid}",
            )
    except Exception as e:
        print(f"[verdict] 复核登记失败（忽略，不影响判定结果）: {e}")


def _persist(verdict: Verdict, job_dir: Path) -> None:
    verdict.created_at = time.time()
    data = verdict.to_dict()
    (job_dir / "verdict.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # events.jsonl 追加 verdict 事件（append-only，不影响既有事件序号语义）
    events_path = job_dir / "events.jsonl"
    try:
        with open(events_path, encoding="utf-8") as rf:
            seq = sum(1 for _ in rf)
        with open(events_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "seq": seq + 1,
                        "job_id": verdict.job_id,
                        "ts": time.time(),
                        "round": 0,
                        "type": "verdict",
                        "payload": data,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError as e:
        print(f"[verdict] events append failed (ignored): {e}")


def render(verdict: Verdict) -> str:
    """人读渲染。"""
    flag = "✅ COMPLETE" if verdict.complete else "❌ INCOMPLETE"
    lines = [
        f"job:        {verdict.job_id}",
        f"判定:       {flag}  (confidence={verdict.confidence:.2f}, method={verdict.method})",
    ]
    if verdict.satisfied_conditions:
        lines.append("满足条件:   " + "；".join(verdict.satisfied_conditions))
    if verdict.evidence_quote:
        lines.append(f"证据引用:   {verdict.evidence_quote[:200]}")
    ac = verdict.anticheat
    if ac:
        lines.append(
            f"反作弊:     答案在工具输出中={ac.get('answer_in_outputs')} "
            f"本地答案直读={ac.get('local_answer_file_read')}"
        )
        for s in ac.get("suspicious") or []:
            lines.append(f"  ⚠ {s}")
    if verdict.reasoning:
        lines.append(f"理由:       {verdict.reasoning[:300]}")
    return "\n".join(lines)
