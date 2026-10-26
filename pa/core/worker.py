"""有界 ReAct 子智能体（BUILD_SPEC §7.5；ARCHITECTURE §8 L3）。

run_worker(task, state, writer, llm, assets) -> WorkerResult。四条硬约束：
A. untrusted 运行时累积：环形缓冲，最近 3 段、每段 ≤2KB，每次工具执行
   后写入摘要；dispatch 时为空（delegate 保证）。
B. 返回的 tried 由调度器并入 task.tried，跨尝试继承——这里从 task.tried
   出发累加，原样返回。
C. status=done 只是主张，最终由 judge()（reflect）裁决。
D. blocked_reason 命中后立即 return，不再发起任何工具调用。
E. M2 批量执行：CallBatch 一轮产出多个动作，每项复用同一条单步管路
   （run_action）逐项过网关 8 步门——批量是打包产出，不是打包放行。

写路径只有两条：clue.added 事件（幂等：按内容去重）与经 gateway 的审计
事件。state 的内存更新与 restore 走同一 apply_event，保证逐字段一致。
"""

from __future__ import annotations

import hashlib
import json
from collections import deque

from pa.core.context import assemble, section_sizes
from pa.core.delegate import compile_briefing
from pa.core.reflect import (
    classify_blocked,
    criteria_met,
    extract_clues,
    is_progress,
)
from pa.errors import ModelCallError, ModelParseError
from pa.gateway import execute
from pa.infra.fp import fingerprint
from pa.infra.llm import complete_retry
from pa.models import (
    CallBatch,
    CallTool,
    Decision,
    Failure,
    Finish,
    Task,
    WorkerResult,
)
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

NO_PROGRESS_LIMIT = 3
UNTRUSTED_RING = 3  # 环形缓冲：最近 3 段
_UNTRUSTED_SEG_MAX = 2 * 1024  # 每段 ≤2KB
# M2 批量上限：单轮 batch 最多执行 8 项。批量密度的失控闸——LLM 一轮
# 产出几十个动作时只执行前 8 个，剩余项靠下一轮反思自然收敛；轮数
# （budget.steps）仍是外层硬界。
_MAX_BATCH = 8
# Finish 随附发现提案的上限：单任务至多 3 条进 JEV，防一口气刷台账。
_MAX_FINISH_FINDINGS = 3
# catch-all 判定：同一 body_sha256 下不同 url 数的门槛
_CATCHALL_MIN_URLS = 3


def _content_scope(tool: str, args: dict) -> str:
    """内容新颖性的主题作用域：同作用域内同 body 才算重复。

    - http：站点（netloc）——同站不同路径同 body 是 SPA 回退；
    - probe/tls：host:port——不同主机/端口的相同结论是新事实；
    - dns：目标域；
    - shell/fs/MCP：空（全局），不同命令的同输出才算重复。"""
    if tool == "http":
        from urllib.parse import urlsplit

        return urlsplit(str(args.get("url") or "")).netloc
    if tool in ("probe", "tls"):
        return f"{args.get('target')}:{args.get('port') or ''}"
    if tool == "dns":
        return str(args.get("target") or "")
    return ""


def _summarize(out) -> str:
    seg = (
        f"exit={out.exit_code} stdout={out.stdout[:1500]!r} stderr={out.stderr[:300]!r}"
    )
    return seg[:_UNTRUSTED_SEG_MAX]


def _propose_finish_findings(
    writer: StoreWriter,
    state,
    task: Task,
    dec: Finish,
    evidence_refs: list[str],
    findings: list,
) -> list:
    """把 Finish 随附的发现提案落成 finding.proposed（finding 管道补全）。

    纪律：
    - evidence_refs 只能绑定本任务产出的证据（至多 3 条）——模型不指定
      ev-id，跨任务引用/编造证据在构造时就不可能发生；
    - 本任务没有产出任何证据时不提案（0 引用会让 JEV 只能给 suspected，
      污染台账；观察类结论走报告的 finish-summary 扫描通道）；
    - 幂等键用完整内容哈希（proposal_key）：同一提案重复收口只落一次，
      补证据/改严重度算新提案，合并语义在投影层。
    """
    if not dec.findings or not evidence_refs:
        return []
    from pa.adjudicate import proposal_key
    from pa.models import FindingProposal
    from pa.store.event import apply_event

    out: list = []
    for f in dec.findings[:_MAX_FINISH_FINDINGS]:
        claim = str(f.claim or "").strip()
        if not claim:
            continue
        proposal = FindingProposal(
            claim=claim[:300],
            severity=f.severity,
            evidence_refs=list(evidence_refs)[:3],
            reproduce=(f.reproduce.strip() or f"重放任务 {task.id} 的动作")[:300],
            falsify_if=[x.strip() for x in f.falsify_if if str(x).strip()][:4]
            or ["证据无法复现或与基线响应一致"],
            origin=[task.id],
        )
        fev = writer.append(
            "finding.proposed",
            f"worker:{task.id}",
            {"finding": proposal.model_dump(mode="json")},
            f"find:{task.id}:{proposal_key(proposal)}",
        )
        if fev is not None:
            apply_event(state, fev)
            findings.append(proposal)
            out.append(proposal)
    return out


def _note(writer: StoreWriter, task: Task, kind: str, step: int, **fields) -> None:
    """把 worker 的「思考节点」写进账本（decision.made，不参与投影）。

    这里刻意只记决策级信息（收口/批量/模型失败/无进展/步数耗尽），
    不记 prompt 正文：prompt 可能含目标与凭据，正文留给 context.assembled
    的尺寸统计与 tui/trace 的按需查看。worker 每个动作的 expect/rationale
    仍随 action.started 落账；两者合起来回答「它为什么这么做」。
    """
    writer.append(
        "decision.made",
        f"worker:{task.id}",
        {
            "kind": kind,
            "task_id": task.id,
            "attempt": task.attempt,
            "step": step,
            **fields,
        },
        f"dm:w:{task.id}:{task.attempt}:{step}:{kind}",
    )


def run_worker(task: Task, state, writer: StoreWriter, llm, assets) -> WorkerResult:
    brief = compile_briefing(state, task, assets, writer.root)
    tried = list(task.tried)  # 约束 B：跨尝试继承
    untrusted: deque[dict] = deque(maxlen=UNTRUSTED_RING)
    history: dict[str, str] = {}  # fp → stdout，判进展用
    produced: list = []
    findings: list = []
    evidence_refs: list[str] = []
    no_progress = 0
    steps_done = 0
    # catch-all 检测的任务级状态：body_sha256 → url 列表
    seen_bodies: dict[str, list[str]] = {}
    catchall_proposed = False
    # 内容新颖性（跨指纹）：出现过的 body_sha256 / stdout 摘要
    seen_content: set[str] = set()

    def result(status, failure: Failure | None = None) -> WorkerResult:
        return WorkerResult(
            task_id=task.id,
            status=status,
            clues=produced,
            findings=findings,
            evidence_refs=evidence_refs,
            tried=tried,
            failure=failure,
            cost={"steps": steps_done},
            confidence=0.9 if status == "done" else 0.5,
        )

    def propose_finding(f) -> None:
        """finding 提案唯一出口：幂等键 = 完整内容哈希（同一提案重复
        落账去重；证据/严重度演化算新提案，合并语义在投影层）。"""
        from pa.adjudicate import proposal_key

        fev = writer.append(
            "finding.proposed",
            f"worker:{task.id}",
            {"finding": f.model_dump(mode="json")},
            f"find:{task.id}:{proposal_key(f)}",
        )
        if fev is not None:
            apply_event(state, fev)
            findings.append(f)

    def run_action(
        dec: CallTool, *, check_criteria: bool = True
    ) -> WorkerResult | None:
        """单个动作的完整单步管路：指纹入 tried → 网关 8 步门 → 证据
        落账 → 线索/敏感暴露投影 → 判据检查 → 信息增益判定。

        为什么抽成单步函数：M2 批量执行（CallBatch）逐项复用同一条
        管路——批量只是「打包产出」，绝不能打包跳门；单动作路径与
        批量路径共享同一实现，杜绝双份语义漂移。返回 WorkerResult
        表示命中出口（blocked / done / partial / needs_help），应立即
        向上返回；返回 None 表示正常继续。"""
        nonlocal no_progress, catchall_proposed

        fp = fingerprint(dec.tool, dec.args)
        tried.append(fp)
        # 新颖性基线必须在动作前快照：pre_kv / pre_content 若在 clue 落账
        # 后计算，本动作刚产生的线索已在集合里 → novel/内容新颖 恒 False
        # （真跑 lake.aimohu.com 的 needs_help 根因之一）。
        pre_kv = {
            (
                c.kind,
                json.dumps(c.value, ensure_ascii=False, sort_keys=True, default=str),
            )
            for c in state.clues
        }
        pre_content = set(seen_content)
        out = execute(
            state,
            writer,
            tool_name=dec.tool,
            args=dec.args,
            task_id=task.id,
            allowed_tools=task.allowed_tools,
            expect=dec.expect,
            rationale=dec.rationale,
        )
        if out.blocked_reason:  # 约束 D：立即返回
            if out.blocked_reason == "budget_exceeded":
                # 裁定 1：budget_exceeded 不是失败——partial，无 Failure
                _note(
                    writer,
                    task,
                    "budget_blocked",
                    steps_done,
                    tool=dec.tool,
                    reason=out.blocked_reason,
                )
                return result("partial")
            _note(
                writer,
                task,
                "blocked",
                steps_done,
                tool=dec.tool,
                reason=out.blocked_reason,
            )
            return result(
                "blocked",
                failure=Failure(
                    cls=classify_blocked(out.blocked_reason),
                    detail=out.blocked_reason,
                    evidence_ref=out.evidence_id,
                ),
            )
        if out.evidence_id:
            evidence_refs.append(out.evidence_id)
        untrusted.append({"source": dec.tool, "data": _summarize(out)})

        new_clues = extract_clues(dec.tool, dec.args, out)
        for c in new_clues:
            c.source_task = task.id
            sig = hashlib.sha256(
                (
                    f"{c.key}|"
                    f"{json.dumps(c.value, ensure_ascii=False, sort_keys=True, default=str)}|"
                    f"{json.dumps(c.meta, ensure_ascii=False, sort_keys=True, default=str)}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            ev = writer.append(
                "clue.added",
                f"worker:{task.id}",
                {"clue": c.model_dump(mode="json")},
                f"clue:{task.id}:{sig}",
            )
            # 只对真正写入的事件投影（幂等命中返回 None）：worker 曾按
            # 内容跳过投影，制造 live/replay 分叉（重放投影、live 不投影，
            # §5.3 可回放约束违反），并让同指纹任务的判据永远等不到归属
            # 线索 → needs_help 空转（缓存命中动作白做）
            if ev is not None:
                apply_event(state, ev)
                produced.append(c)
        # 确定性安全规则（规则兜底，不依赖模型自觉）——产出 finding 交
        # JEV 裁决，与 sensitive_finding 同一管道。顺序敏感：catch-all
        # 检测先于 sensitive——同批内第三条敏感路径命中时，抑制已经生效。
        rule_findings: list = []
        if dec.tool == "http" and out.evidence_id:
            from pa.core import secrules
            from pa.core.sensitive import sensitive_finding

            meta = getattr(out, "meta", None) or {}
            url = str(dec.args.get("url") or "")
            # 1) catch-all：≥3 条路径同构 200 且含敏感形态路径
            sha = str((meta or {}).get("body_sha256") or "")
            if sha and 200 <= int(out.exit_code) < 300:
                urls = seen_bodies.setdefault(sha, [])
                if url not in urls:
                    urls.append(url)
                if (
                    not catchall_proposed
                    and len(urls) >= _CATCHALL_MIN_URLS
                    and secrules.has_sensitive_path(urls)
                ):
                    catchall_proposed = True
                    cf = secrules.catchall_finding(urls, out.evidence_id)
                    cf.origin = [task.id]
                    rule_findings.append(cf)
            # 2) sensitive：敏感路径 2xx/3xx（catch-all 确认后抑制，防误报）
            if not catchall_proposed:
                f = sensitive_finding(url, out.exit_code, out.evidence_id)
                if f is not None:
                    f.origin = [task.id]  # 溯源边（§8.3-4）：谁产出的
                    rule_findings.append(f)
            # 3) header：缺失安全响应头
            if meta:
                hf = secrules.header_finding(
                    url,
                    int(out.exit_code),
                    meta.get("headers") or {},
                    out.evidence_id,
                )
                if hf is not None:
                    hf.origin = [task.id]
                    rule_findings.append(hf)
        if dec.tool == "tls" and out.exit_code == 0 and out.evidence_id:
            from pa.core import secrules

            fields = next(
                (
                    c.value
                    for c in new_clues
                    if c.kind == "tls.cert" and isinstance(c.value, dict)
                ),
                {},
            )
            fields.setdefault(
                "key", f"{dec.args.get('target')}:{dec.args.get('port') or 443}"
            )
            for f in secrules.tls_findings(fields, out.evidence_id):
                f.origin = [task.id]
                rule_findings.append(f)
        for f in rule_findings:
            propose_finding(f)

        if check_criteria and criteria_met(
            state.clues, task.success_criteria, task_id=task.id
        ):
            _note(
                writer,
                task,
                "criteria_met",
                steps_done,
                criteria=[c.evidence for c in task.success_criteria],
            )
            return result("done")
        # 信息增益（联调裁定 + 内容新颖修正）：catch-all 站点让「换个
        # 随机路径再试」每次都产生新 key 的线索，(kind, value) 已观测过
        # → 不算新信息（早停保留）。但同状态不同正文的响应是真进展
        # （http 的 Outcome.exit_code=200，不能拿 exit_code==0 判成功）——
        # 用 body_sha256（无 meta 退 stdout 摘要）判内容新颖，作用域按
        # 主题：http 按站点（同站同 body 的换路径不算）、probe/tls 按
        # host:port（不同主机是不同事实）、其余全局。pre_kv / pre_content
        # 是动作前快照（函数开头），此处不得重算。
        novel = any(
            (
                c.kind,
                json.dumps(c.value, ensure_ascii=False, sort_keys=True, default=str),
            )
            not in pre_kv
            for c in new_clues
        )
        meta = getattr(out, "meta", None) or {}
        body = str(meta.get("body_sha256") or "") or str(out.stdout)[:2000]
        body_sig = f"{_content_scope(dec.tool, dec.args)}|{body}" if body else ""
        if body_sig and out.ok:
            seen_content.add(body_sig)
        if is_progress(
            out,
            dec.expect,
            new_clue=novel,
            fp=fp,
            history=history,
            body_sig=body_sig or None,
            seen_content=pre_content,
        ):
            no_progress = 0
        else:
            no_progress += 1
            if no_progress >= NO_PROGRESS_LIMIT:
                _note(
                    writer,
                    task,
                    "needs_help",
                    steps_done,
                    no_progress=no_progress,
                    limit=NO_PROGRESS_LIMIT,
                )
                return result("needs_help")
        history[fp] = out.stdout
        return None

    for step in range(task.budget.steps):
        steps_done = step + 1
        payload = dict(brief)
        payload["clues"] = brief["relevant_clues"]
        payload["untrusted_data"] = list(untrusted)  # 约束 A：运行时累积
        system, user = assemble("worker", state, payload, assets)
        # S13 观测事件：只记各段尺寸与占比——不记正文、不记目标、不记凭据，
        # 供 stats 的 prompts 斜率 / worker 步数分布使用
        writer.append(
            "context.assembled",
            f"worker:{task.id}",
            {
                "task_id": task.id,
                "attempt": task.attempt,
                "step": steps_done,
                "phase": "worker",
                **section_sizes("worker", state, payload, assets),
            },
            f"ctx:{task.id}:{task.attempt}:{steps_done}",
        )
        try:
            raw = complete_retry(
                llm,
                retries=1,
                system=system,
                user=user,
                schema=Decision,
                phase="worker",
            )
            dec = llm.parse(raw, Decision)
        except (ModelParseError, ModelCallError) as e:
            # 裁定 2：解析/调用失败不许上抛——长任务里这是常态。
            # 返回 failed + Model 失败，由引擎决定降级。
            _note(
                writer,
                task,
                "model_error",
                steps_done,
                error_type=type(e).__name__,
                detail=str(e)[:300],
            )
            return result("failed", failure=Failure(cls="Model", detail=str(e)[:200]))

        if isinstance(dec, Finish):
            proposed = _propose_finish_findings(
                writer, state, task, dec, evidence_refs, findings
            )
            _note(
                writer,
                task,
                "finish",
                steps_done,
                summary=dec.summary[:500],
                satisfied=list(dec.satisfied),
                rationale=dec.rationale[:500],
                findings_proposed=len(proposed),
                findings_skipped=max(0, len(dec.findings) - len(proposed)),
            )
            return result("done")
        # M2 批量执行：一轮可产出多个动作（CallBatch），对齐裁定「批量
        # 只是打包产出、不是打包放行」——每一项都走 run_action 的完整
        # 单步管路（指纹、8 步门、证据、线索、判据），绝不批量跳门。
        # 单 CallTool 即长度为 1 的列表，预计划代码路径不变。
        acts: list[CallTool] = []
        is_batch = isinstance(dec, CallBatch)
        if is_batch:
            acts = list(dec.actions)[:_MAX_BATCH]
            _note(
                writer,
                task,
                "batch",
                steps_done,
                count=len(acts),
                requested=len(dec.actions),
                rationale=dec.rationale[:500],
            )
        elif isinstance(dec, CallTool):
            acts = [dec]
        if not acts:
            _note(
                writer,
                task,
                "empty_batch",
                steps_done,
                rationale=getattr(dec, "rationale", "")[:300],
            )
            continue  # worker 只认 CallTool / batch / Finish
        for act in acts:
            # 批量是「一批动作」，不是「命中第一条就收工」：批内先逐项
            # 执行，全部完成后再统一判据。否则枚举类任务（多端口/多路径）
            # 会被第一条 open/200 截断，计划 8 条只跑 1 条。
            stop = run_action(act, check_criteria=not is_batch)
            if stop is not None:
                return stop
        if is_batch and criteria_met(
            state.clues, task.success_criteria, task_id=task.id
        ):
            _note(
                writer,
                task,
                "criteria_met",
                steps_done,
                criteria=[c.evidence for c in task.success_criteria],
            )
            return result("done")

    _note(
        writer,
        task,
        "steps_exhausted",
        steps_done,
        budget_steps=task.budget.steps,
    )
    return result("partial")  # 步数耗尽：partial，不是 done
