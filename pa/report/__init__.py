"""report 包：证据链报告渲染与自动落盘（S11 约束 B–F：确定性 / 正文只放 confirmed）。"""

from __future__ import annotations

from pathlib import Path

from pa.infra.log import get_logger
from pa.models import MissionState
from pa.report.render import render_report
from pa.store.view import StoreView

__all__ = ["render_report", "save_report"]

logger = get_logger("secpt.report")


def save_report(state: MissionState, root: Path) -> Path | None:
    """终态自动落盘 report.md（WORKFLOW §7 b8：真跑结束后自动生成）。

    为什么挂在 run() 收口单点：resume-finished（重放后已是终态，循环
    顶部 break）、enforce 提前收口、正常 check_termination 收口——三条
    路径都在 run() 的 return 前汇聚，一处挂钩全部覆盖；follow.watch 仍
    只打印不生成（L211 原文口径）。

    幂等性：render_report 是确定性的（约束 B），同一 state 写两次逐字节
    相等，崩溃重入安全；resume 场景重放后文件被重写为相同内容——不是
    破坏，是收敛到同一终态产物。

    家族级聚合（runs/N）：root 是 runs/N 时，家族根（root.parent.parent）
    的 report.md 聚合所有历史 run 的 findings + 证据链。report.md 始终
    落家族根，用户不需要记 runs/N 的具体编号。

    降级：报告是交付产物不是运行依赖，渲染/写盘任何异常都静默降级
    （返回 None + logging 告警），不打断主循环的退出路径；账本无损，
    可事后手动 `secpt report` 补救。SECPT_DIE_AT_STEP 断电钩子在报告
    挂钩之前提前退出（收口轮中途断电），此时不产生报告——恢复路径
    （resume 二跑）会补上，这正是挂钩放在 run() 而非 _finish 里的
    原因：_finish 在断电时刻也执行不到报告，统一由 return 前收口。
    """
    try:
        root = Path(root)
        if root.parent.name == "runs":
            # runs/N 子账本：家族级聚合所有历史 run，report.md 落家族根
            family = root.parent.parent
            text = _family_report(family, root, state)
            out = family / "report.md"
        else:
            view = StoreView(root)
            text = render_report(state, view)
            out = root / "report.md"
        out.write_text(text, encoding="utf-8")
        return out
    except Exception as e:  # noqa: BLE001 —— 边界产物落盘，任何失败都降级
        logger.warning("终态报告落盘失败，已跳过（可手动 secpt report 补救）: %s", e)
        return None


def _family_report(family: Path, latest: Path, state: MissionState) -> str:
    """聚合所有 runs/N 的 findings 成家族级 report.md。

    口径：同一 finding（asset_id + fingerprint 相同）在所有 run 中只出现
    一次，裁决取「最新一次提交的 verdict」；证据引用跨 run 共享（内容寻址）。
    家族根的 report.md 始终以「最新一次 run」的视角开头（概述、验收、
    发现正文），历史 run 的 findings 按时间序附在「历史 run」一节之后。
    """
    from pa.store.derive import finding_fingerprint, merge_finding

    runs = sorted(
        (p for p in (family / "runs").iterdir() if p.name.isdigit()),
        key=lambda p: int(p.name),
    )
    merged: dict[tuple[str | None, str], object] = {}
    # 按 run 顺序重放：后跑的 run 覆盖同指纹 finding 的 verdict / 证据链，
    # merge_finding 合并 origin 并集（§9.2-3）
    for run in runs:
        rv = StoreView(run)
        if not rv.exists():
            continue
        rs = rv.restore()
        for f in rs.findings:
            key = (f.asset_id, finding_fingerprint(f))
            if key in merged:
                # 同指纹已存在：用 merge_finding 的「内容以最新为准、
                # severity 降杆、origin 并集」规则刷新
                probe = MissionState(
                    spec=rs.spec, mission_id=rs.mission_id, findings=[merged[key]]
                )
                merge_finding(probe, f)
                merged[key] = probe.findings[0]
            else:
                merged[key] = f.model_copy(deep=True)
    # 家族级裁决：同 finding_id 取最新一次非空 verdict
    adjudications: dict[str, object] = {}
    for run in runs:
        rv = StoreView(run)
        if not rv.exists():
            continue
        rs = rv.restore()
        for fid, adj in rs.adjudications.items():
            adjudications[fid] = adj
    # 家族级 clues：合并所有 run 的 clue（去重按 (kind, key, value)）
    clues: dict[tuple[str, str, str], object] = {}
    for run in runs:
        rv = StoreView(run)
        if not rv.exists():
            continue
        rs = rv.restore()
        for c in rs.clues:
            clues[(c.kind, c.key, str(c.value))] = c
    # 用合并后的状态渲染家族报告：view 指向 latest（本次跑开的 runs/N），
    # 证据引用从这个账本核验（内容寻址，跨 run 共享同一证据文件）
    merged_state = MissionState(
        spec=state.spec,
        mission_id=state.mission_id,
        tasks=state.tasks,
        findings=list(merged.values()),
        adjudications=adjudications,
        clues=list(clues.values()),
        spent_tokens=state.spent_tokens,
        spent_seconds=state.spent_seconds,
        step=state.step,
        finished=state.finished,
        finish_summary=state.finish_summary,
        finish_reason=state.finish_reason,
        meta=state.meta,
    )
    return render_report(merged_state, StoreView(latest))
