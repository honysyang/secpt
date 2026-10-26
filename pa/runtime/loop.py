"""主循环（ARCHITECTURE §8 L4）：引导 / 恢复 → 轮询直到终态。

两条入口合一（S10 约束 A）：目录里已有 events.jsonl → resume() 重放续跑；
否则写 mission.json + mission.created + bootstrap_plan 引导。

测试钩子 SECPT_DIE_AT_STEP=N：第 N 个超级步结算后 os._exit(9)，模拟断电；
接着对同一目录再跑一次 run()（恢复路径），最终状态与不中断跑完全一致
（spent_seconds 除外——墙钟不可重放）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pa.assets.snapshot import AssetSnapshot
from pa.core.planner import bootstrap_plan
from pa.errors import SecptError
from pa import envs
from pa.models import AcceptanceItem, MissionSpec, MissionState, now
from pa.runtime import budget, scheduler
from pa.runtime.recovery import resume
from pa.store.event import apply_event
from pa.store.view import StoreView
from pa.store.writer import StoreWriter

PLATEAU_LIMIT = 8                    # CONTROL_PLANE §6.2：连续 8 轮无新线索

_PROMPT_DIR = Path(__file__).resolve().parents[1] / "core" / "prompts"
_SKILL_DIRS = [Path(__file__).resolve().parents[2] / "skills"]


def run(root, spec: MissionSpec | None = None, *, llm=None,
        assets=None) -> MissionState:
    root = Path(root)
    if llm is None:
        raise SecptError("run() 需要 llm 客户端（MockLLM 或 ApiLLM）")
    if assets is None:
        assets = AssetSnapshot.freeze(_PROMPT_DIR, _SKILL_DIRS)
    view = StoreView(root)
    if view.exists():
        state, writer = resume(root)          # 恢复路径：账本重放 + 租约回收
    else:
        if spec is None:
            raise SecptError("新目录引导需要 spec（mission.json 不存在）")
        state, writer = _bootstrap(root, spec, llm, assets)
    cursor = [0]                              # llm.calls 消费位
    started = time.monotonic()
    while True:
        if state.finished:
            break                     # enforce 下 mentor 可能已提前收口
        reason = check_termination(state)
        if reason:
            _finish(state, writer, reason)
            break
        scheduler.round(state, writer, llm, assets)
        budget.charge(state, writer, llm, cursor, started)
        started = time.monotonic()
        # meta 由 scheduler.round 内重算（judge 后 coach 前，裁定 1.3）。
        # 终态检查的 no_progress 因此滞后一个结算——纯时序差，账本口径不变
        writer.checkpoint(state)
        die_at = envs.die_at_step()
        if die_at is not None and state.step == die_at:
            os._exit(9)                       # 测试钩子：模拟第 N 步断电
    return state


def _bootstrap(root: Path, spec: MissionSpec, llm, assets):
    root.mkdir(parents=True, exist_ok=True)
    (root / "mission.json").write_text(
        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8")
    writer = StoreWriter(root)
    state = MissionState(spec=spec, mission_id=root.name)
    writer.append("mission.created", "engine",
                  {"objective": spec.objective, "targets": spec.targets},
                  f"mc:{root.name}")
    bootstrap_plan(state, writer, llm, assets)
    return state, writer


def check_termination(state: MissionState) -> str | None:
    """CONTROL_PLANE §6.2 顺序：预算 → 平台期 → 截止；扩展一条成功出口：
    任务排空且验收达成 → completed。返回 finish_reason，未终止返回 None。

    平台期（§6.2 裁定 2）：no_progress ≥ 8 且不存在「state==ready 且
    attempt < MAX_ATTEMPTS」的任务——不能只看 ready_tasks()，失败重排队
    会打破它；带上界后 thrashing 任务停在 failed，不再挡住平台期。"""
    if state.finished:
        return None
    b = state.spec.budget
    if b.tokens and state.spent_tokens >= b.tokens:
        return "budget_tokens"
    if b.steps and state.step >= b.steps:
        return "budget_steps"
    if _acceptance_met(state):
        # 裁定 1.1：只看验收达成——满足验收后无重试余地不再是前提，
        # 否则队列里还有 ready 任务时会继续空跑烧预算
        return "completed"
    if _queue_drained(state):
        # P1-1：本轮无 ready 且无 running → 立即 plateau（原因随
        # _plateau_reason 区分 thrashing / no_ready），不等 8 轮
        return "plateau"
    if state.meta.get("no_progress_steps", 0) >= PLATEAU_LIMIT \
            and not _has_retryable_work(state):
        return "plateau"
    if state.spec.deadline and now() > state.spec.deadline:
        return "deadline"
    return None


def _has_retryable_work(state: MissionState) -> bool:
    return any(t.state == "ready" and t.attempt < scheduler.MAX_ATTEMPTS
               for t in state.tasks.values())


def _plateau_reason(state: MissionState) -> str:
    """平台期收口原因（§6.2 裁定 3）：有任务把尝试次数耗尽仍失败 =
    thrashing（无效重试）；其余（blocked / superseded / 从未就绪）=
    no_ready（任务排空、验收未达成）。"""
    if any(t.state == "failed" and t.attempt >= scheduler.MAX_ATTEMPTS
           for t in state.tasks.values()):
        return "thrashing"
    return "no_ready"


def _acceptance_hit(c, a: AcceptanceItem) -> bool:
    """判定委托 pa.core.evidence_kinds.acceptance_hit（唯一出口，口径
    见该函数 docstring）；此处保留包装以兼容既有调用与测试导入。"""
    from pa.core.evidence_kinds import acceptance_hit
    return acceptance_hit(c, a)


def _acceptance_met(state: MissionState) -> bool:
    """每项验收各自独立满足（AND）。target 把每项钉在特定对象上，
    不再有「同族证据命中其一即可」的折叠——不同对象的同 kind 项是
    不同的验收要求。空验收视为已满足（与旧 requirements_met 口径一致）。"""
    return all(any(_acceptance_hit(c, a) for c in state.clues)
               for a in state.spec.acceptance)


def _item_met(state: MissionState, a: AcceptanceItem) -> bool:
    return any(_acceptance_hit(c, a) for c in state.clues)


def _queue_drained(state: MissionState) -> bool:
    """队列排空（P1-1）：无可派发（无 ready）且无在跑（无 running），
    也没有下一轮会重排队回来的失败任务 → 立即收口，不等 8 轮空烧预算。
    P0-2 之后 deps 不再造成 pending 滞留；blocked（out_of_scope 等不可
    复活）与 failed（尝试耗尽）都是永久态，留着它们不会改变任何事。"""
    from pa.graph.ready import ready_tasks
    if ready_tasks(state):              # 含依赖/前置已满足的 pending
        return False
    if any(t.state == "running" for t in state.tasks.values()):
        return False
    if any(t.state == "failed" and t.attempt < scheduler.MAX_ATTEMPTS
           for t in state.tasks.values()):
        return False                    # 下轮重排队，队列还会活
    return True


def _finish(state: MissionState, writer: StoreWriter, reason: str) -> None:
    satisfied = [a.id for a in state.spec.acceptance if _item_met(state, a)]
    done = sum(1 for t in state.tasks.values() if t.state == "done")
    summary = (f"mission 结束（{reason}）："
               f"任务 {done}/{len(state.tasks)} done，"
               f"验收满足 {len(satisfied)}/{len(state.spec.acceptance)}，"
               f"findings {len(state.findings)}")
    payload = {"summary": summary, "reason": reason, "satisfied": satisfied}
    if reason == "plateau":
        # 随事件落盘：纯重放（无快照）也能还原，不靠内存残留
        payload["plateau_reason"] = _plateau_reason(state)
        state.meta["plateau_reason"] = payload["plateau_reason"]
    ev = writer.append("mission.finished", "engine", payload,
                       f"mf:{state.mission_id}")
    apply_event(state, ev)
    writer.snapshot_now(state)
