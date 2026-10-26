"""主循环（ARCHITECTURE §8 L4）：引导 / 恢复 → 轮询直到终态。

两条入口合一（S10 约束 A）：目录里已有 events.jsonl → resume() 重放续跑；
否则写 mission.json + mission.created + bootstrap_plan 引导。

测试钩子 SECPT_DIE_AT_STEP=N：第 N 个超级步结算后 os._exit(9)，模拟断电；
接着对同一目录再跑一次 run()（恢复路径），最终状态与不中断跑完全一致
（spent_seconds 除外——墙钟不可重放）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from pa.assets.snapshot import AssetSnapshot
from pa.core import audit, experience
from pa.core.planner import bootstrap_plan
from pa.errors import SecptError
from pa.infra.log import get_logger
from pa.report import save_report
from pa.graph.ready import ready_tasks
from pa import envs
from pa.models import AcceptanceItem, Clue, MissionSpec, MissionState, now
from pa.runtime import budget, scheduler, wakeup as wakeup_mod
from pa.runtime.recovery import resume
from pa.store.event import apply_event
from pa.store.view import StoreView
from pa.store.writer import StoreWriter

PLATEAU_LIMIT = 8  # CONTROL_PLANE §6.2：连续 8 轮无新线索

logger = get_logger("secpt.loop")

_PROMPT_DIR = Path(__file__).resolve().parents[1] / "core" / "prompts"
_SKILL_DIRS = [
    Path(__file__).resolve().parents[2] / "assets" / "skills",
    Path(__file__).resolve().parents[2] / "assets" / "playbooks",
]
_ROLE_DIRS = (Path(__file__).resolve().parents[2] / "assets" / "roles",)


def run(
    root, spec: MissionSpec | None = None, *, llm=None, assets=None
) -> MissionState:
    root = Path(root)
    if llm is None:
        raise SecptError("run() 需要 llm 客户端（MockLLM 或 ApiLLM）")
    if assets is None:
        assets = AssetSnapshot.freeze(_PROMPT_DIR, _SKILL_DIRS, _ROLE_DIRS)
    view = StoreView(root)
    if view.exists():
        state, writer = resume(root)  # 恢复路径：账本重放 + 租约回收
        # 只有账本确实进入过执行（有任务）才继续；只有 mission.created /
        # planner context 的半成品（规划期中断）要重新 bootstrap。否则
        # 0 任务会被 check_termination 误判成 plateau，生成一份“0 发现”
        # 的正常报告（真跑 runs/2 事故）。
        if not state.finished and not state.tasks:
            bootstrap_plan(state, writer, llm, assets)
            if not state.tasks:
                _finish(state, writer, "failed", llm=llm)
                save_report(state, root)
                return state
    else:
        if spec is None:
            raise SecptError("新目录引导需要 spec（mission.json 不存在）")
        state, writer = _bootstrap(root, spec, llm, assets)
        if not state.tasks:
            _finish(state, writer, "failed", llm=llm)
            save_report(state, root)
            return state
    cursor = [0]  # llm.calls 消费位
    started = time.monotonic()
    while True:
        if state.finished:
            break  # enforce 下 mentor 可能已提前收口
        reason = check_termination(state)
        if reason:
            _finish(state, writer, reason, llm=llm)
            break
        # 信号级唤醒（WORKFLOW §4.1）：扫账本未消化窗口得唤醒包；
        # dirty（有信号）或队列有活才走完整 round，否则心跳兜底。
        wake = wakeup_mod.collect(writer)
        if wake.signals or ready_tasks(state):
            scheduler.round(state, writer, llm, assets, wake=wake)
        else:
            scheduler.heartbeat(state, writer)
        wakeup_mod.save(writer)  # 水位推进到当前账头
        budget.charge(state, writer, llm, cursor, started)
        started = time.monotonic()
        # meta 由 scheduler.round 内重算（judge 后 coach 前，裁定 1.3）。
        # 终态检查的 no_progress 因此滞后一个结算——纯时序差，账本口径不变
        writer.checkpoint(state)
        die_at = envs.die_at_step()
        if die_at is not None and state.step == die_at:
            os._exit(9)  # 测试钩子：模拟第 N 步断电
    # 终态自动落盘报告（WORKFLOW §7 b8）：三条收口路径（resume-finished /
    # enforce 提前收口 / check_termination 正常收口）在这里汇聚，单点挂钩
    # 全覆盖；失败静默降级不阻断退出（save_report docstring）。断电路径
    # 走不到这里——由 resume 二跑补报告，账本口径不变。
    if state.finished:
        save_report(state, root)
    return state


def _scan_attachments(state: MissionState, writer: StoreWriter) -> None:
    """bootstrap 附件确定性扫描（0day 研究第一遍）。

    每个附件：规则命中 → evidence 落盘 + finding.proposed（JEV 管道）
    + cred 线索（报告脱敏）；无论命中与否都落 file.scan 线索——这是纯
    文件任务的验收锚点（「附件已扫描并有结论」）。幂等：clue 内容签名
    与 finding proposal_key 双保险，resume 重放不重复落账。"""
    from pa.adjudicate import proposal_key
    from pa.core import filerules
    from pa.infra import evidence
    from pa.store.event import apply_event

    for ref in state.spec.files:
        try:
            hits, dep_hits, binary = filerules.scan_attachment(writer.root, ref)
        except Exception as e:  # noqa: BLE001 —— 扫描失败不阻塞引导
            logger.warning("附件扫描失败 %s: %s", ref.name, e)
            continue
        clue = filerules.clue_for(ref, hits, len(dep_hits), binary)
        sig = hashlib.sha256(
            f"{clue.key}|{json.dumps(clue.value, ensure_ascii=False, sort_keys=True)}".encode()
        ).hexdigest()[:16]
        ev = writer.append(
            "clue.added",
            "engine",
            {"clue": clue.model_dump(mode="json")},
            f"clue:engine:file:{sig}",
        )
        if ev is not None:
            apply_event(state, ev)
        if binary:
            continue  # 二进制文件：入 workspace 但不按行扫描
        for hit in hits:
            eid = evidence.put(
                writer.root,
                json.dumps(
                    {
                        "tool": "file_scan",
                        "args": {"file": ref.name, "rule": hit.rule},
                        "exit": 0,
                        "stdout": f"{ref.name}:{hit.line_no}: {hit.snippet}",
                        "stderr": "",
                        "task_id": None,
                        "meta": {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8"),
                task_id=None,
            )
            writer.append(
                "evidence.stored", "engine", {"id": eid, "task_id": None}, f"ev:{eid}"
            )
            if hit.secret:
                # cred 线索：报告层自动脱敏（前 4 位），台账正文只存已
                # 脱敏摘要——秘密明文不复制进账本/报告。
                ccred = Clue(
                    kind="cred",
                    key=f"{ref.basename}:{hit.line_no}",
                    value=hit.snippet,
                    evidence_id=eid,
                    target=ref.basename,
                    source_task=None,
                )
                csig = hashlib.sha256(
                    f"{ccred.key}|{ccred.value}".encode()
                ).hexdigest()[:16]
                cev = writer.append(
                    "clue.added",
                    "engine",
                    {"clue": ccred.model_dump(mode="json")},
                    f"clue:engine:cred:{csig}",
                )
                if cev is not None:
                    apply_event(state, cev)
            f = filerules.finding_for(ref, hit, eid)
            fev = writer.append(
                "finding.proposed",
                "engine",
                {"finding": f.model_dump(mode="json")},
                f"find:file-scan:{proposal_key(f)}",
            )
            if fev is not None:
                apply_event(state, fev)
        for adv in dep_hits:
            eid = evidence.put(
                writer.root,
                json.dumps(
                    {
                        "tool": "file_scan",
                        "args": {"file": ref.name, "rule": "advisory_match"},
                        "exit": 0,
                        "stdout": (
                            f"{ref.name}: {adv.get('name')}@{adv.get('version')} "
                            f"命中 {adv.get('id')}（fixed {adv.get('fixed')}）"
                        ),
                        "stderr": "",
                        "task_id": None,
                        "meta": {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8"),
                task_id=None,
            )
            writer.append(
                "evidence.stored", "engine", {"id": eid, "task_id": None}, f"ev:{eid}"
            )
            f = filerules.advisory_finding(ref, adv, eid)
            fev = writer.append(
                "finding.proposed",
                "engine",
                {"finding": f.model_dump(mode="json")},
                f"find:file-scan:{proposal_key(f)}",
            )
            if fev is not None:
                apply_event(state, fev)


def _bootstrap(root: Path, spec: MissionSpec, llm, assets):
    root.mkdir(parents=True, exist_ok=True)
    (root / "mission.json").write_text(
        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    writer = StoreWriter(root)
    # mission_id 用家族根目录名（do-<hash8>），不用账本目录名（runs/N）——
    # 家族内多次跑共享同一 mission_id，跨 run 合并 findings 时身份一致
    state = MissionState(
        spec=spec,
        mission_id=root.parent.parent.name if root.parent.name == "runs" else root.name,
    )
    writer.append(
        "mission.created",
        "engine",
        {"objective": spec.objective, "targets": spec.targets},
        f"mc:{state.mission_id}",
    )
    # 附件确定性扫描在 planner 之前：findings/线索先上白板，planner
    # 引导轮的 lessons/advisories 注入能看到它们，规划才有依据。
    _scan_attachments(state, writer)
    bootstrap_plan(state, writer, llm, assets)
    return state, writer


def check_termination(state: MissionState) -> str | None:
    """CONTROL_PLANE §6.2 顺序：预算 → 平台期 → 截止；成功出口（联调
    裁定）：验收达成且队列排空 → completed。返回 finish_reason，未终止
    返回 None。

    收口口径（联调裁定）：验收满足但队列未排空不收工——继续把活干完
    （实测「completed 时 t-0001 从未派发、findings 0」的浅层完成），
    直到队列排空/预算耗尽/卡住无进展，再按验收达成与否判 completed
    或 plateau。

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
    if not state.tasks:
        # 0 任务不是“平台期”，是引导失败：不能生成正常渗透报告。
        return "failed"
    drained = _queue_drained(state)
    met = _acceptance_met(state)
    if drained:
        # 收口口径（联调裁定）：验收达成是「下限满足」不是「活干完了」——
        # 队列未排空继续跑（findings 的机会），排空后才判 completed；
        # 未达成则立即 plateau（P1-1），不等 8 轮
        return "completed" if met else "plateau"
    stuck = state.meta.get(
        "no_progress_steps", 0
    ) >= PLATEAU_LIMIT and not _has_retryable_work(state)
    if stuck:
        # 卡住时验收已达成 = 合同满足且无进展余量，按成功收口；
        # 未达成 = plateau（§6.2 裁定 2，带上界重排后不挡平台期）
        return "completed" if met else "plateau"
    if state.spec.deadline and now() > state.spec.deadline:
        return "deadline"
    return None


def _has_retryable_work(state: MissionState) -> bool:
    return any(
        t.state == "ready" and t.attempt < scheduler.MAX_ATTEMPTS
        for t in state.tasks.values()
    )


def _plateau_reason(state: MissionState) -> str:
    """平台期收口原因（§6.2 裁定 3）：有任务把尝试次数耗尽仍失败 =
    thrashing（无效重试）；其余（blocked / superseded / 从未就绪）=
    no_ready（任务排空、验收未达成）。"""
    if any(
        t.state == "failed" and t.attempt >= scheduler.MAX_ATTEMPTS
        for t in state.tasks.values()
    ):
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
    return all(
        any(_acceptance_hit(c, a) for c in state.clues) for a in state.spec.acceptance
    )


def _item_met(state: MissionState, a: AcceptanceItem) -> bool:
    return any(_acceptance_hit(c, a) for c in state.clues)


def _queue_drained(state: MissionState) -> bool:
    """队列排空（P1-1）：无可派发（无 ready）且无在跑（无 running），
    也没有下一轮会重排队回来的失败任务 → 立即收口，不等 8 轮空烧预算。
    P0-2 之后 deps 不再造成 pending 滞留；blocked（out_of_scope 等不可
    复活）与 failed（尝试耗尽）都是永久态，留着它们不会改变任何事。"""
    from pa.graph.ready import ready_tasks

    if ready_tasks(state):  # 含依赖/前置已满足的 pending
        return False
    if any(t.state == "running" for t in state.tasks.values()):
        return False
    if any(
        t.state == "failed" and t.attempt < scheduler.MAX_ATTEMPTS
        for t in state.tasks.values()
    ):
        return False  # 下轮重排队，队列还会活
    return True


def _finish(state: MissionState, writer: StoreWriter, reason: str, *, llm=None) -> None:
    # 出口审计（M3）：交付门前核验全部 finding 的证据链完整性，先落
    # mission.audit 再落 mission.finished——账本读起来「审计在先、收口
    # 在后」；idem ma:{mission_id}，同一 mission 不重复落账。审计结论
    # 不进投影表（快照语义），报告按当前证据盒重算。返回值作为经验
    # 提炼的机械信号快照。
    audit_summary = audit.exit_audit(state, writer)
    # E3 收口提炼钩子：exit_audit 之后、mission.finished 之前——断电
    # 重入时 mission 尚未 finished 会再走 _finish，distill 幂等靠
    # origin 字段兜底不重复落条；llm 缺席/调用失败全部静默降级。
    experience.distill(
        state, writer.root, llm, finish_reason=reason, audit_summary=audit_summary
    )
    satisfied = [a.id for a in state.spec.acceptance if _item_met(state, a)]
    done = sum(1 for t in state.tasks.values() if t.state == "done")
    summary = (
        f"mission 结束（{reason}）："
        f"任务 {done}/{len(state.tasks)} done，"
        f"验收满足 {len(satisfied)}/{len(state.spec.acceptance)}，"
        f"findings {len(state.findings)}"
    )
    payload = {"summary": summary, "reason": reason, "satisfied": satisfied}
    if reason == "plateau":
        # 随事件落盘：纯重放（无快照）也能还原，不靠内存残留
        payload["plateau_reason"] = _plateau_reason(state)
        state.meta["plateau_reason"] = payload["plateau_reason"]
    ev = writer.append("mission.finished", "engine", payload, f"mf:{state.mission_id}")
    if ev is not None:
        apply_event(state, ev)
    writer.snapshot_now(state)
