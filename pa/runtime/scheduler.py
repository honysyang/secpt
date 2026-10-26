"""调度器（ARCHITECTURE §8 L4；编排权迁移裁定）——缩编后的原语库。

编排决策已移交 pa.core.planner.orchestrate（decide + 确定性护栏）；
调度器只保留模型拿不走、拿错了会崩的机械门禁，一轮内固定顺序：

  revive_blocked → 失败重排队（带上界） → 就绪提升 → orchestrate 选批
  → 派发 → merge(tried 并回) → judge → 状态落定 → JEV sweep
  → meta 重算 → coach → 轮末简报（E2）。

选批的语义来自 orchestrate 的返回批（受 mission 决策或确定性兜底
fallback_directive 驱动——模型缺席/给坏指令时兜底接管，引擎不会
因为一个决策失败而瘫痪）。

JEV sweep 只做事编排（找未裁决 finding → 调 pa/adjudicate 的规则 →
落账），裁决规则本身在 pa/adjudicate/rules.py，可单测（S11 约束 A）。

失败重排队上界（§6.2 裁定）：failed → ready 仅当 attempt < MAX_ATTEMPTS，
达到上界停在 failed——否则无效重试会把平台期永远挡在「还有 ready 任务」
之外，只能靠预算收口。
"""

from __future__ import annotations

from pa.adjudicate import adjudicate, finding_id, unlock_hint
from pa.core.memory import reflect_round
from pa.core.planner import orchestrate
from pa.core.reflect import judge
from pa.core.worker import run_worker
from pa.graph.ready import block_dep_failed, promote_pending, revive_blocked
from pa.graph.state import transition
from pa.models import Adjudication, MissionState
from pa.policy import coach
from pa.runtime.lease import make_lease
from pa.runtime.wakeup import WakeUp
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

MAX_ATTEMPTS = 3  # 单任务最大尝试次数（§6.2 裁定 1）


def _requeue_failed(state: MissionState, writer: StoreWriter) -> None:
    """失败重排队（round 与 heartbeat 共用的机械动作）：仅
    attempt < MAX_ATTEMPTS；restart.decided 落账，restarts_same_task
    由 derive_stats 重算。幂等键 rs:{id}:{attempt} 防重复落账。"""
    for t in state.tasks.values():
        if t.state == "failed" and t.attempt < MAX_ATTEMPTS:
            transition(t, "ready", writer)
            writer.append(
                "restart.decided",
                "engine",
                {"task_id": t.id, "attempt": t.attempt},
                f"rs:{t.id}:{t.attempt}",
            )


def round(
    state: MissionState, writer: StoreWriter, llm, assets, *, wake: WakeUp | None = None
) -> None:
    """完整调度轮。wake 为信号级唤醒包（可选 kwarg，兼容四参数调用）：
    语义不变——多带一个「台账预消化」入口，见步骤 2.5。"""
    # 1) 机械门禁：复活 dangling_dep 阻塞任务（确定性，无决策权）
    revive_blocked(state, writer)
    # 1.5) 失败重排队：仅 attempt < MAX_ATTEMPTS（上界见模块 docstring）
    _requeue_failed(state, writer)
    # 1.8) 依赖已死传播：dep 打满/阻塞/废止后，下游 pending 不再静默滞留
    block_dep_failed(state, writer, MAX_ATTEMPTS)
    # 2) 就绪提升：满足依赖与前置的 pending → ready（编排输入由此就绪）
    promote_pending(state, writer)
    # 2.5) finding 置顶预消化（WORKFLOW §4.1 纪律 ②）：唤醒包含
    #      finding 类信号时，先裁决台账再编排——legacy 的 jev_sweep
    #      只做事编排、幂等、无编排权，提前到 orchestrate 前不改变
    #      engine 主循环语义；轮末步骤 4 仍跑（本轮新提案兜底）。
    if wake is not None and wake.finding:
        jev_sweep(state, writer)
    # 3) 编排决策：planner decide（护栏收口；模型失败静默降级兜底批）
    batch = orchestrate(state, writer, llm, assets)
    for task in batch:
        _dispatch(task, state, writer, llm, assets)
    # 4) JEV sweep：本轮新产生的 + 历轮漏判的 finding 一并裁决（规则在
    #    pa/adjudicate，这里只编排与落账）
    jev_sweep(state, writer)
    # 5) 行为参数本轮重算（裁定 1.3：judge 之后、coach 之前——coach 的
    #    advisory.metric 必须含本轮失败，不得读上一轮的陈旧 meta）
    from pa.store.derive import derive_stats
    from pa.store.view import StoreView

    state.meta.update(derive_stats(StoreView(writer.root).events(from_seq=1)))
    # 6) 教练检查：shadow 只 advise，enforce 追加三个动作之一
    coach.on_round(state, writer)
    # 7) 轮末滚动简报（E2）：LLM 提炼本轮印象；失败静默降级不阻塞收工
    #    （§5.2 红线 3——简报是增强信息不是决策依据，MockLLM 未注入
    #    "reflect" phase 的现有测试自动走降级路径）
    reflect_round(state, writer, llm)


def _dispatch(task, state: MissionState, writer: StoreWriter, llm, assets) -> None:
    attempt = task.attempt + 1
    transition(
        task,
        "running",
        writer,
        lease=make_lease(task),
        attempt=attempt,
        tried=task.tried,
    )
    writer.append(
        "task.dispatched",
        "engine",
        {"task_id": task.id, "attempt": attempt},
        f"td:{task.id}:{attempt}",
    )
    result = run_worker(task, state, writer, llm, assets)
    writer.append(
        "worker.returned",
        f"worker:{task.id}",
        {
            "task_id": task.id,
            "status": result.status,
            "failure": (
                result.failure.model_dump(mode="json") if result.failure else None
            ),
        },
        f"wr:{task.id}:{attempt}",
    )
    # 5) merge(tried 并回)：跨尝试继承，保序去重
    seen: set[str] = set()
    merged: list[str] = []
    for fp in list(task.tried) + list(result.tried):
        if fp not in seen:
            seen.add(fp)
            merged.append(fp)
    task.tried = merged
    # 6) judge（status=done 只是主张）；7) 状态落定（CONTROL_PLANE §6.2 矩阵）
    verdict = judge(task, result, state)
    if verdict in ("success", "false_failure"):
        transition(task, "done", writer, tried=task.tried)
    elif verdict == "virtual_success":
        transition(task, "failed", writer, reason="virtual_success", tried=task.tried)
    elif verdict == "blocked":
        transition(
            task,
            "blocked",
            writer,
            reason=result.failure.detail if result.failure else "blocked",
            tried=task.tried,
        )
    else:  # failure / partial → failed
        transition(
            task,
            "failed",
            writer,
            reason=result.failure.cls if result.failure else "partial",
            tried=task.tried,
        )


def jev_sweep(state: MissionState, writer: StoreWriter) -> list[str]:
    """裁决所有未裁决的 finding，返回本轮新裁决的 finding_id 列表。

    二验信号（PentAGI auditor 模式的 coach 化落地）：高危 finding 只有
    单条证据时必然 suspected——这是「一句话定高危」的最高风险形态。
    引擎不擅自建任务（编排权在智能体侧），但必须把「该补一次独立验证」
    作为 coach 信号写上白板，orchestrator 上下文（advisories 段）可见。"""
    from pa.adjudicate.rules import CONFIRMED_MIN_REFS
    from pa.store.view import StoreView

    view = StoreView(writer.root)
    done: list[str] = []
    for f in state.findings:
        fid = finding_id(f)
        if fid in state.adjudications:
            continue
        verdict, reason = adjudicate(view, f)
        adj = Adjudication(
            finding_id=fid,
            verdict=verdict,
            reason=reason,
            verdict_by="jev-v0",
            unlock=unlock_hint(verdict, f),
        )
        ev = writer.append(
            "finding.adjudicated", "engine", adj.model_dump(mode="json"), f"adj:{fid}"
        )
        if ev is not None:
            apply_event(state, ev)
        done.append(fid)
        if (
            verdict == "suspected"
            and f.severity in ("high", "critical")
            and len(f.evidence_refs) < CONFIRMED_MIN_REFS
        ):
            writer.append(
                "mentor.advisory",
                "mentor",
                {
                    "level": "I2",
                    "issue": "verify_finding",
                    "metric": {"finding_id": fid, "refs": len(f.evidence_refs)},
                    "threshold": {"refs": CONFIRMED_MIN_REFS},
                    "would_do": "spawn_verify_task",
                    "target": fid,
                    "converged_after": None,
                    "occurrences": 1,
                },
                f"adv:verify:{fid}",
            )
    return done


def heartbeat(state: MissionState, writer: StoreWriter) -> None:
    """兜底闹钟心跳（WORKFLOW §4.1 纪律 ③）：信号唤醒不替代机械停滞
    检查——本函数只做机械动作，无 orchestrate / 无 dispatch / 无 LLM /
    无 derive / 无 coach。

    为何必须包含重排队 + 提升（死锁推演）：若只做 revive+jev，状态机
    会以「retryable failed 存在（drained=False 不终止）+ 无信号 + 心跳
    不重排队」无限空转——budget.charge P2-2 对零 LLM 轮不结算，derive
    的 no_progress 无人驱动，check_termination 永不收口。重排队把
    failed 抬回 ready，下一轮因 ready 非空自然进入完整 round，链路闭合。

    重排队落的是信号级事件（task.state_changed / restart.decided），
    但调用方（loop）在心跳路同样推进水位——信号语义已由「ready 非空
    → round」这一循环条件表达，水印不留尾巴。"""
    revive_blocked(state, writer)
    _requeue_failed(state, writer)
    block_dep_failed(state, writer, MAX_ATTEMPTS)
    promote_pending(state, writer)
    jev_sweep(state, writer)
