"""secpt analysis：每次运行自动落盘的分析文档（面向系统优化）。

与 report.md 的分工：
- report.md = 交付物（验收核对矩阵 / findings / 证据链），客户视角；
- analysis.md = 研究记录（全过程 + 系统信号 + 失败归因 + 优化建议），
  回答「这次运行系统表现如何、下一次怎么改」。

写入时机：run() 任意终态收口时自动写 `<账本目录>/analysis.md`
（runs/N/analysis.md——每个 run 一份，家族级聚合的 report.md 不覆盖
它）；`secpt report <dir>` 会同时重新生成两份。确定性：同一账本两次
渲染逐字节一致（不引 now()/随机数，墙钟取自事件 ts）。

内容复用：执行过程叙事直接复用 pa.report.log.render_log（思考/行动/
观察/判定全链），本文档不重写第二份叙事——单一事实源，log 改了
analysis 跟着对。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from pa.infra.log import get_logger
from pa.models import MissionState
from pa.report import log as log_mod
from pa.report import stats as stats_mod
from pa.report.follow import _EXIT
from pa.store.view import StoreView

logger = get_logger("secpt.analysis")

_EXIT_NAME = {
    0: "0（completed）",
    1: "1（error/failed）",
    2: "2（budget）",
    3: "3（plateau）",
    4: "4（halted）",
}


def _family_label(root: Path) -> str:
    root = Path(root)
    if root.parent.name == "runs":
        return f"{root.parent.parent.name}/{root.name}"
    return root.name


def _fmt_k(n: int) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _run_signals(root: Path) -> dict:
    """运行级系统信号：账本事件里数出来的行为特征。全部确定性。"""
    fallback_rounds = 0
    abandoned = 0
    batch_truncated = 0
    bad_criteria = 0
    cached_echoes = 0
    needs_help = 0
    dep_blocked = 0
    verify_signals = 0
    finish_findings = 0
    for ev in StoreView(root).events():
        t, p = ev.type, ev.payload
        if t == "decision.made":
            if p.get("kind") == "orchestrate" and str(p.get("mode", "")).startswith(
                "fallback:"
            ):
                fallback_rounds += 1
            elif p.get("kind") == "finish":
                finish_findings += int(p.get("findings_proposed") or 0)
        elif t == "mentor.intervened" and p.get("issue") == "bad_criterion":
            bad_criteria += 1
        elif t == "mentor.advisory":
            if p.get("issue") == "verify_finding":
                verify_signals += 1
        elif t == "action.finished" and p.get("cached"):
            cached_echoes += 1
        elif t == "worker.returned":
            if p.get("status") == "needs_help":
                needs_help += 1
        elif t == "task.state_changed" and str(p.get("reason") or "").startswith(
            "dep_failed"
        ):
            dep_blocked += 1
    # abandon 计数在 decision.made payload 里（abandoned 列表）
    for ev in StoreView(root).events():
        if ev.type == "decision.made" and ev.payload.get("kind") == "orchestrate":
            abandoned += len(ev.payload.get("abandoned") or [])
    # 批量截断：decision.made create_dropped 不区分原因，另看 batch note
    for ev in StoreView(root).events():
        if ev.type == "decision.made" and ev.payload.get("kind") == "batch":
            req, cnt = int(ev.payload.get("requested") or 0), int(
                ev.payload.get("count") or 0
            )
            if req > cnt:
                batch_truncated += req - cnt
    return {
        "fallback_rounds": fallback_rounds,
        "abandoned": abandoned,
        "batch_truncated": batch_truncated,
        "bad_criteria": bad_criteria,
        "cached_echoes": cached_echoes,
        "needs_help": needs_help,
        "dep_blocked": dep_blocked,
        "verify_signals": verify_signals,
        "finish_findings": finish_findings,
    }


def _failure_attribution(state: MissionState, root: Path) -> tuple[list[str], dict]:
    """失败归因：每任务的状态/尝试/失败原因（worker.returned 汇总）。"""
    fails: dict[str, list[str]] = {}
    for ev in StoreView(root).events():
        if ev.type != "worker.returned":
            continue
        tid = str(ev.payload.get("task_id") or "?")
        f = ev.payload.get("failure") or {}
        status = str(ev.payload.get("status") or "?")
        if f:
            fails.setdefault(tid, []).append(
                f"{status}({f.get('cls')}: {str(f.get('detail'))[:80]})"
            )
        elif status in ("needs_help", "partial"):
            fails.setdefault(tid, []).append(status)
    rows: list[str] = []
    by_class: Counter[str] = Counter()
    for tid in sorted(state.tasks):
        t = state.tasks[tid]
        reasons = fails.get(tid) or []
        reason_s = "；".join(reasons[-2:]) if reasons else "-"
        line = f"- {tid} [{t.type}] {t.state}（attempt {t.attempt}）{reason_s}"
        if t.block_reason and t.state == "blocked":
            line += f" block_reason={t.block_reason}"
        rows.append(line)
        if t.state == "failed" and reasons:
            for r in reasons:
                cls = r.split("(")[1].split(":")[0] if "(" in r else r
                by_class[cls] += 1
    return rows, dict(by_class)


def _optimization_tips(state: MissionState, row: dict, sig: dict) -> list[str]:
    """运行级优化建议：stats.suggest 之上，叠加本次运行的具体行为观察。"""
    tips: list[str] = list(stats_mod.suggest(stats_mod.aggregate([row])))
    if sig["fallback_rounds"]:
        tips.append(
            f"编排降级 {sig['fallback_rounds']} 轮（fallback:*）——"
            "模型可用性/输出格式不稳定，检查端点或换 JSON mode。"
        )
    if sig["bad_criteria"]:
        tips.append(
            f"合同门拒绝 {sig['bad_criteria']} 条不可判定判据——"
            "planner/orchestrator 提示词需要更强的新判据示例。"
        )
    if sig["cached_echoes"]:
        tips.append(
            f"缓存命中 {sig['cached_echoes']} 次——worker 在重复已试动作，"
            "检查 tried/last_failure 注入是否生效。"
        )
    if sig["needs_help"]:
        tips.append(
            f"needs_help {sig['needs_help']} 次——信息增益判空转："
            "任务判据与工具产出能力不匹配，查 content_scope 规则。"
        )
    if sig["dep_blocked"]:
        tips.append(
            f"依赖失败传播 {sig['dep_blocked']} 次——上游任务失败连带下游，"
            "考虑减少非必要 deps 或更早换路线。"
        )
    if sig["batch_truncated"]:
        tips.append(
            f"批量截断 {sig['batch_truncated']} 项——单轮 batch 超过 8 上限，"
            "枚举规模应拆任务或用覆盖度判据。"
        )
    if sig["abandoned"]:
        tips.append(
            f"编排者废止 {sig['abandoned']} 个任务——换路线在发生；"
            "确认替代任务真的换了方法而不是换措辞。"
        )
    if not state.findings and row["tokens"] > 0:
        tips.append(
            f"零 finding 消耗 {_fmt_k(row['tokens'])} tokens——"
            "检查验收是否只覆盖探测类判据、规则引擎是否命中、"
            "worker 是否用 finish.findings 提案。"
        )
    if sig["finish_findings"] == 0 and state.tasks:
        tips.append(
            "没有任何 worker 收口提案——在 worker.md 里强化"
            "「有证据的结论必须随 finish.findings 提交」。"
        )
    return tips


def render_analysis(state: MissionState, root: Path) -> str:
    """运行分析文档：元信息 → 系统信号 → 优化建议 → 失败归因 → 全过程。"""
    root = Path(root)
    view = StoreView(root)
    events = list(view.events())
    row = stats_mod.collect(root)
    sig = _run_signals(root)
    lines: list[str] = []
    w = lines.append

    w(f"# secpt 运行分析 {_family_label(root)}")
    w("")
    w("> 本文档面向**系统优化**：记录一次运行的全过程、系统信号与失败归因。")
    w("> 交付物见同目录 report.md（家族级）；原始账本 events.jsonl。")
    w("")
    w("## 运行元信息")
    w("")
    w(f"- 运行：{_family_label(root)}")
    w(f"- 目标：{'、'.join(state.spec.targets)}")
    w(f"- 任务：{state.spec.objective}")
    b = state.spec.budget
    w(
        f"- 预算：steps≤{b.steps} · tokens≤{_fmt_k(b.tokens)} · "
        f"seconds≤{b.seconds} · risk={b.risk}"
    )
    if state.spec.files:
        w("- 附件：" + "、".join(f.name for f in state.spec.files))
    pr = state.meta.get("plateau_reason")
    reason = state.finish_reason or "未完成"
    w(f"- 终态：{reason}" + (f"（{pr}）" if pr else ""))
    w(f"- 退出码：{_EXIT.get(reason, 1)} {_EXIT_NAME.get(_EXIT.get(reason, 1), '')}")
    w(
        f"- 结算：{row['steps']} 步 · {_fmt_k(row['tokens'])} tokens · "
        f"墙钟 {row['wall_s']}s"
    )
    w("")

    w("## 系统信号")
    w("")
    t = row["tasks"]
    w(
        f"- 任务分布：done {t.get('done', 0)} / failed {t.get('failed', 0)} / "
        f"blocked {t.get('blocked', 0)} / pending {t.get('pending', 0)}"
    )
    f = row.get("findings_confirmed", 0)
    s_ = row.get("findings_suspected", 0)
    fp = row.get("findings_false_positive", 0)
    w(f"- findings 裁决：confirmed {f} · suspected {s_} · false_positive {fp}")
    w(
        f"- 质量信号：virtual_success {row['vs_events']} 次 · "
        f"Model 解析失败 {row['model_errors']} 次 · AuthZ 阻塞 {row['authz']} 次 · "
        f"restart {sum(1 for e in events if e.type == 'restart.decided')} 次"
    )
    w(
        f"- 行为信号：needs_help {sig['needs_help']} · 缓存命中 {sig['cached_echoes']} · "
        f"编排降级轮 {sig['fallback_rounds']} · 废止任务 {sig['abandoned']} · "
        f"依赖失败传播 {sig['dep_blocked']} · 合同门拒绝 {sig['bad_criteria']}"
    )
    w(f"- prompt 尺寸斜率：{row['prompt_slope']} 字符/步")
    w("")

    w("## 优化建议")
    w("")
    tips = _optimization_tips(state, row, sig)
    for tip in tips:
        w(f"- {tip}")
    if not tips:
        w("- 无（所有指标在阈值内）")
    w("")

    w("## 失败归因")
    w("")
    rows, by_class = _failure_attribution(state, root)
    if by_class:
        w(
            "- 失败分类："
            + "、".join(f"{k} {v}" for k, v in sorted(by_class.items()))
        )
    for line in rows:
        w(line)
    w("")

    # 执行过程全记录：复用 render_log（单一事实源），剥掉其一级标题
    body = log_mod.render_log(root)
    parts = body.split("\n", 1)
    w("## 运行过程全记录")
    w("")
    w(parts[1].lstrip("\n") if len(parts) > 1 else body)
    return "\n".join(lines).rstrip() + "\n"


def save_analysis(state: MissionState, root: Path) -> Path | None:
    """run() 任意终态时自动落盘 analysis.md（与 save_report 同款降级：
    失败告警不阻断退出）。写到账本目录本身——每个 run 一份。"""
    try:
        root = Path(root)
        text = render_analysis(state, root)
        out = root / "analysis.md"
        out.write_text(text, encoding="utf-8")
        return out
    except Exception as e:  # noqa: BLE001 —— 边界产物落盘，任何失败都降级
        logger.warning("运行分析文档落盘失败（可手动 secpt report 补救）: %s", e)
        return None
