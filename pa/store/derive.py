"""行为统计重算 + 台账合并投影（裁定 2 / WORKFLOW §9.2-3）。

state.meta 里的五个行为参数是派生缓存，唯一真源是事件账本：restore() 用
本模块从全量事件重算；运行期不存在单独维护这些参数的写路径。任何重启、
删快照，重放后都必须得到同样的值。

findings 的重复合并也下沉在这里：merge_finding 是台账投影语义（apply_event
的 finding.proposed 分支调用），账本本身保持 append-only 的原始提案流——
合并只作用于内存投影，trace 取证视图完整保留每次提案。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from pa.models import Event, FindingProposal, MissionState


def derive_stats(events: Iterable[Event]) -> dict[str, Any]:
    """从事件流重算五个行为参数，返回可直接并入 state.meta 的 dict。"""
    no_progress = 0
    saw_clue = False
    plan_rewrites = 0
    planned = 0
    virtual_success = 0
    worker_returns = 0
    failures_by_class: dict[str, int] = {}
    restarts: dict[str, int] = {}
    fp_attempts: dict[str, int] = {}
    for ev in events:
        t = ev.type
        if t == "clue.added":
            saw_clue = True
        elif t == "budget.charged":
            # 一个超级步结算一次 budget.charged：有新线索则清零，否则累加
            no_progress = 0 if saw_clue else no_progress + 1
            saw_clue = False
        elif t == "plan.created":
            planned += int(ev.payload.get("n", 0))
        elif t == "plan.revised":
            plan_rewrites += 1
        elif t == "action.finished":
            # §6.5.1 失败重试计数：非 cached 的失败执行才计数，
            # cached 命中（含 cache_hit_exhausted）只是审计回声，不计
            if not ev.payload.get("cached") and int(ev.payload.get("exit", 0)) != 0:
                fp = str(ev.payload.get("fp", ""))
                fp_attempts[fp] = fp_attempts.get(fp, 0) + 1
        elif t == "worker.returned":
            worker_returns += 1
            failure = ev.payload.get("failure")
            if failure:
                cls = str(failure.get("cls", "?"))
                failures_by_class[cls] = failures_by_class.get(cls, 0) + 1
        elif t == "task.state_changed":
            to = ev.payload.get("to")
            frm = ev.payload.get("from")
            tid = str(ev.payload.get("task_id", ""))
            if to == "ready" and frm in ("running", "failed"):
                restarts[tid] = restarts.get(tid, 0) + 1
            if to == "failed" and ev.payload.get("reason") == "virtual_success":
                virtual_success += 1
    return {
        "no_progress_steps": no_progress,
        "plan_rewrites": plan_rewrites,
        "restarts_same_task": max(restarts.values()) if restarts else 0,
        "virtual_success_count": virtual_success,  # vs_events：按事件计（教练用）
        "worker_returns": worker_returns,
        "failures_by_class": dict(sorted(failures_by_class.items())),
        "planned": planned,
        "fp_attempts": fp_attempts,
    }


def advisory_occurrences(events: Iterable[Event]) -> dict[str, int]:
    """1.4：同 issue+target 的命中次数（首次 mentor.advisory 记 occurrences，
    其后每条 mentor.advisory_agg{delta:1} 累加）。

    故意不进 state.meta：meta 任一键差都会让 off/shadow 两个 mission 的
    状态 dump 不同（S12 验收一要求 shadow 与 off 逐字节一致）。occurrences
    是教练的内部判据，谁要用谁从账本现算，口径唯一且可重放。"""
    occ: dict[str, int] = {}
    for ev in events:
        if ev.type == "mentor.advisory":
            key = f"{ev.payload.get('issue')}:{ev.payload.get('target')}"
            occ[key] = max(int(occ.get(key, 0)), int(ev.payload.get("occurrences", 1)))
        elif ev.type == "mentor.advisory_agg":
            key = f"{ev.payload.get('issue')}:{ev.payload.get('target')}"
            occ[key] = int(occ.get(key, 0)) + int(ev.payload.get("delta", 1))
    return occ


def finding_fingerprint(f: FindingProposal) -> str:
    """发现的内容稳定键（合并用）：只哈希「这条发现说的是哪件事」。

    finding_id 是全内容哈希——evidence/severity 一变 id 就跟着变，拿它当
    合并键会让同一暴露面被重复建档（真跑 §0 质量项：两次 /.git 各得一个
    fid）。claim/reproduce/asset_id 才是跨提案稳定的身份字段；其余
    （severity/evidence_refs/falsify_if/origin）都会随合并演化，被剔除。
    """
    blob = json.dumps(
        {"claim": f.claim, "reproduce": f.reproduce, "asset_id": f.asset_id},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


_SEV_RANK = {"critical": 5, "high": 4, "med": 3, "low": 2, "info": 1}


def merge_finding(state: MissionState, new: FindingProposal) -> bool:
    """台账投影的重复发现合并（WORKFLOW §9.2-3），返回是否发生了合并。

    同 (asset_id, fingerprint) 的提案不再逐条堆叠：
    - 内容（claim/reproduce/evidence_refs/falsify_if）以最新一次提交为准
      —— 随最新证据演进；
    - severity 降杆：取与旧条目相比更保守者，防同一发现被重复提案把
      严重度越刷越高；
    - origin 溯源边（§8.3-4）做并集——交付报告与出口审计（M3）可回答
      「谁出的这条」。

    只改内存投影、不写账本；重放逐条走同一函数，live/replay 收敛一致。
    """
    for i, old in enumerate(state.findings):
        if old.asset_id == new.asset_id and finding_fingerprint(
            old
        ) == finding_fingerprint(new):
            merged = new.model_copy(deep=True)
            old_rank = _SEV_RANK.get(old.severity, 0)
            new_rank = _SEV_RANK.get(new.severity, 0)
            merged.severity = old.severity if old_rank < new_rank else new.severity
            merged.origin = sorted(set(old.origin) | set(new.origin))
            state.findings[i] = merged
            return True
    state.findings.append(new)
    return False
