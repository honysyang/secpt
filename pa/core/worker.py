"""有界 ReAct 子智能体（BUILD_SPEC §7.5；ARCHITECTURE §8 L3）。

run_worker(task, state, writer, llm, assets) -> WorkerResult。四条硬约束：
A. untrusted 运行时累积：环形缓冲，最近 3 段、每段 ≤2KB，每次工具执行
   后写入摘要；dispatch 时为空（delegate 保证）。
B. 返回的 tried 由调度器并入 task.tried，跨尝试继承——这里从 task.tried
   出发累加，原样返回。
C. status=done 只是主张，最终由 judge()（reflect）裁决。
D. blocked_reason 命中后立即 return，不再发起任何工具调用。

写路径只有两条：clue.added 事件（幂等：按内容去重）与经 gateway 的审计
事件。state 的内存更新与 restore 走同一 apply_event，保证逐字段一致。
"""
from __future__ import annotations

import hashlib
import json
from collections import deque

from pa.core.context import assemble
from pa.core.delegate import compile_briefing
from pa.core.reflect import (
    classify_blocked, criteria_met, extract_clues, is_progress,
)
from pa.errors import ModelCallError, ModelParseError
from pa.gateway import execute
from pa.infra.fp import fingerprint
from pa.infra.llm import complete_retry
from pa.models import (
    CallTool, Decision, Failure, Finish, Task, WorkerResult,
)
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

NO_PROGRESS_LIMIT = 3
UNTRUSTED_RING = 3                    # 环形缓冲：最近 3 段
_UNTRUSTED_SEG_MAX = 2 * 1024         # 每段 ≤2KB


def _summarize(out) -> str:
    seg = (f"exit={out.exit_code} stdout={out.stdout[:1500]!r} "
           f"stderr={out.stderr[:300]!r}")
    return seg[:_UNTRUSTED_SEG_MAX]


def run_worker(task: Task, state, writer: StoreWriter, llm, assets) -> WorkerResult:
    brief = compile_briefing(state, task, assets)
    tried = list(task.tried)                        # 约束 B：跨尝试继承
    untrusted: deque[dict] = deque(maxlen=UNTRUSTED_RING)
    history: dict[str, str] = {}                    # fp → stdout，判进展用
    produced: list = []
    evidence_refs: list[str] = []
    no_progress = 0
    steps_done = 0

    def result(status, failure: Failure | None = None) -> WorkerResult:
        return WorkerResult(task_id=task.id, status=status, clues=produced,
                            evidence_refs=evidence_refs, tried=tried,
                            failure=failure, cost={"steps": steps_done},
                            confidence=0.9 if status == "done" else 0.5)

    for step in range(task.budget.steps):
        steps_done = step + 1
        payload = dict(brief)
        payload["clues"] = brief["relevant_clues"]
        payload["untrusted_data"] = list(untrusted)   # 约束 A：运行时累积
        system, user = assemble("worker", state, payload, assets)
        try:
            raw = complete_retry(llm, retries=1, system=system, user=user,
                                 schema=Decision, phase="worker")
            dec = llm.parse(raw, Decision)
        except (ModelParseError, ModelCallError) as e:
            # 裁定 2：解析/调用失败不许上抛——长任务里这是常态。
            # 返回 failed + Model 失败，由引擎决定降级。
            return result("failed",
                          failure=Failure(cls="Model", detail=str(e)[:200]))

        if isinstance(dec, Finish):
            return result("done")
        if not isinstance(dec, CallTool):
            continue                              # worker 只认 CallTool / Finish

        fp = fingerprint(dec.tool, dec.args)
        tried.append(fp)
        out = execute(state, writer, tool_name=dec.tool, args=dec.args,
                      task_id=task.id,
                      allowed_tools=task.allowed_tools)
        if out.blocked_reason:                    # 约束 D：立即返回
            if out.blocked_reason == "budget_exceeded":
                # 裁定 1：budget_exceeded 不是失败——partial，无 Failure
                return result("partial")
            return result("blocked",
                          failure=Failure(cls=classify_blocked(out.blocked_reason),
                                          detail=out.blocked_reason,
                                          evidence_ref=out.evidence_id))
        if out.evidence_id:
            evidence_refs.append(out.evidence_id)
        untrusted.append({"source": dec.tool, "data": _summarize(out)})

        new_clues = extract_clues(dec.tool, dec.args, out)
        pre_keys = {c.key for c in state.clues}
        existing = {(c.key, json.dumps(c.value, ensure_ascii=False,
                                       sort_keys=True, default=str))
                    for c in state.clues}
        for c in new_clues:
            c.source_task = task.id
            sig = hashlib.sha256(
                f"{c.key}|{json.dumps(c.value, ensure_ascii=False, sort_keys=True, default=str)}"
                .encode("utf-8")).hexdigest()[:16]
            ev = writer.append("clue.added", f"worker:{task.id}",
                               {"clue": c.model_dump(mode="json")},
                               f"clue:{task.id}:{sig}")
            if (c.key, json.dumps(c.value, ensure_ascii=False, sort_keys=True,
                                  default=str)) not in existing:
                apply_event(state, ev)            # 幂等去重时不再重复投影
                existing.add((c.key, json.dumps(
                    c.value, ensure_ascii=False, sort_keys=True, default=str)))
                produced.append(c)

        if criteria_met(state.clues, task.success_criteria):
            return result("done")
        fresh = any(c.key not in pre_keys for c in new_clues)
        if is_progress(out, dec.expect, new_clue=fresh, fp=fp,
                       history=history):
            no_progress = 0
        else:
            no_progress += 1
            if no_progress >= NO_PROGRESS_LIMIT:
                return result("needs_help")
        history[fp] = out.stdout

    return result("partial")                      # 步数耗尽：partial，不是 done
