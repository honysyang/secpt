"""证据链报告渲染（BUILD_SPEC §7.7 / S11 约束 B–F）。

硬约束：
B. 确定性——无时间戳、无随机顺序；findings 按 (severity desc, id asc)；
   同一 state 渲染两次必须逐字节相等（spent_seconds 等墙钟量不入口）。
C. 正文只放 confirmed；suspected / false_positive 进附录，false_positive
   必须写清「为什么排除」（给甲方的可信度证明，取裁决 reason）。
D. 每条 finding 附 evidence_refs + 每个 ref 的 sha256 前 12 位，便于客户
   独立校验证据库。
E. 「未达成项与原因」一节恒存在——缺它客户只看到「没打穿」。
F. 凭证类线索（kind=="cred"）脱敏为前 4 位；成文后再做一次全文扫描替换，
   防止 finding 正文回引凭证明文。
"""
from __future__ import annotations

from pa.models import MissionState
from pa.store.view import StoreView

_SEV_RANK = {"critical": 5, "high": 4, "med": 3, "low": 2, "info": 1}


def _sorted_findings(state: MissionState):
    def key(f):
        from pa.adjudicate import finding_id
        return (-_SEV_RANK.get(f.severity, 0), finding_id(f))
    return sorted(state.findings, key=key)


def _mask_cred(value) -> str:
    s = str(value)
    return s[:4] + "…" if len(s) > 4 else s[:1] + "…"


def render_report(state: MissionState, view: StoreView) -> str:
    lines: list[str] = []
    w = lines.append
    w("# secpt 渗透测试报告")
    w("")
    w("## 概述")
    w("")
    w(f"- 目标：{'、'.join(state.spec.targets)}")
    reason = state.finish_reason or "未完成"
    pr = state.meta.get("plateau_reason")
    w(f"- 终止原因：{reason}" + (f"（{pr}）" if pr else ""))
    satisfied = _satisfied(state)
    w(f"- 验收：{len(satisfied)}/{len(state.spec.acceptance)} 项达成")
    counts: dict[str, int] = {}
    for t in state.tasks.values():
        counts[t.state] = counts.get(t.state, 0) + 1
    w(f"- 任务：{len(state.tasks)}（"
      + "、".join(f"{k} {v}" for k, v in sorted(counts.items())) + "）")
    verdicts = [a.verdict for a in state.adjudications.values()]
    w(f"- 发现：{len(state.findings)}（confirmed {verdicts.count('confirmed')}、"
      f"suspected {verdicts.count('suspected')}、"
      f"false_positive {verdicts.count('false_positive')}）")
    w(f"- 结算：step {state.step}，tokens {state.spent_tokens}")
    w("")
    w("## 已达成验收项")
    w("")
    if satisfied:
        for a in satisfied:
            w(f"- {a.id}：{a.item}")
    else:
        w("- 无")
    w("")
    w("## 发现（confirmed）")
    w("")
    confirmed = [f for f in _sorted_findings(state)
                 if state.adjudications.get(
                     _fid(f), _UNADJUDICATED_SENTINEL).verdict == "confirmed"]
    if confirmed:
        for f in confirmed:
            _render_finding(w, state, view, f)
    else:
        w("- 无已确认发现")
    w("")
    w("## 未达成项与原因")
    w("")
    unmet = [a for a in state.spec.acceptance
             if not _item_met(state, a)]
    if unmet:
        for a in unmet:
            w(f"- {a.id}：{a.item}")
            w(f"  - 原因：无任何线索命中判据 evidence={a.evidence}")
            related = [t for t in state.tasks.values()
                       if not (t.state == "done")]
            if related:
                desc = "；".join(
                    f"{t.id}（{t.state}"
                    + (f"，原因 {t.block_reason}" if t.block_reason else "")
                    + "）" for t in sorted(related, key=lambda x: x.id))
                w(f"  - 未完成任务：{desc}")
    else:
        w("- 无（全部达成）")
    w("")
    w("## 线索摘要")
    w("")
    creds: list[str] = []
    if state.clues:
        for c in sorted(state.clues, key=lambda c: (c.kind, c.key)):
            if c.kind == "cred":
                creds.append(str(c.value))
                w(f"- [{c.kind}] {c.key} = {_mask_cred(c.value)}（已脱敏，仅前 4 位）")
            else:
                w(f"- [{c.kind}] {c.key} = {c.value}")
    else:
        w("- 无")
    w("")
    _render_appendix(w, state, view)
    text = "\n".join(lines) + "\n"
    # 二次防线：finding 正文若回引了凭证明文，一并替换成脱敏形态
    for full in creds:
        if len(full) > 4:
            text = text.replace(full, _mask_cred(full))
    return text


class _Unadjudicated:
    verdict = "suspected"
    reason = "未裁决（调度轮未覆盖）"


_UNADJUDICATED_SENTINEL = _Unadjudicated()


def _fid(f) -> str:
    from pa.adjudicate import finding_id
    return finding_id(f)


def _satisfied(state):
    return [a for a in state.spec.acceptance if _item_met(state, a)]


def _item_met(state, a) -> bool:
    # 与主循环收口同一判定出口（evidence_kinds.acceptance_hit）——
    # 旧的宽松比对（只看 key/value 撞 evidence）曾让同一账本出现
    # 「收口说 3/6、报告说 0/6」的自相矛盾
    from pa.core.evidence_kinds import acceptance_hit
    return any(acceptance_hit(c, a) for c in state.clues)


def _ref_line(view, ref: str) -> str:
    if not view.evidence_verified(ref):
        return f"`{ref}`（无法核验）"
    sha = str(view.evidence_meta(ref).get("sha", ""))[:12]
    return f"`{ref}`（sha256 前 12 位：{sha}）"


def _render_finding(w, state, view, f) -> None:
    adj = state.adjudications.get(_fid(f), _UNADJUDICATED_SENTINEL)
    w(f"### F-{_fid(f)} [{f.severity}] {f.claim}")
    w("")
    w(f"- 裁决：{adj.verdict}（{adj.reason}）")
    w("- 证据链：")
    for ref in f.evidence_refs:
        w(f"  - {_ref_line(view, ref)}")
    if not f.evidence_refs:
        w("  - （无证据引用）")
    w(f"- 复现步骤：{f.reproduce}")
    w(f"- 证伪条件：{'；'.join(f.falsify_if) if f.falsify_if else '未声明'}")
    w("")


def _render_appendix(w, state, view) -> None:
    for verdict, title in (("suspected", "附录 A：疑似发现（suspected）"),
                           ("false_positive", "附录 B：已排除发现（false_positive）")):
        w(f"## {title}")
        w("")
        fs = [f for f in _sorted_findings(state)
              if state.adjudications.get(
                  _fid(f), _UNADJUDICATED_SENTINEL).verdict == verdict]
        if not fs:
            w("- 无")
            w("")
            continue
        for f in fs:
            adj = state.adjudications.get(_fid(f), _UNADJUDICATED_SENTINEL)
            w(f"### F-{_fid(f)} [{f.severity}] {f.claim}")
            w("")
            if verdict == "false_positive":
                w(f"- 为什么排除：{adj.reason}")
            else:
                w(f"- 裁决理由：{adj.reason}")
            w("- 证据链：")
            for ref in f.evidence_refs:
                w(f"  - {_ref_line(view, ref)}")
            if not f.evidence_refs:
                w("  - （无证据引用）")
            w(f"- 复现步骤：{f.reproduce}")
            w(f"- 证伪条件：{'；'.join(f.falsify_if) if f.falsify_if else '未声明'}")
            w("")
