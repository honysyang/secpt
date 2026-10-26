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

from pa.core.audit import audit_refs
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


# 验收核对：区分“观察到了”与“预期被验证”。旧报告把同族线索当成
# 达成（301 被 200 满足），客户会误以为预期状态被证实。
_VERIFIED = "VERIFIED"
_CONFLICT = "CONTRACT_CONFLICT"
_NOT_OBSERVED = "NOT_OBSERVED"


def _host_of(target: str) -> str:
    return str(target or "").split(":", 1)[0]


def _matching_clues(a, clues):
    host = _host_of(getattr(a, "target", ""))
    expected = str(getattr(a, "evidence", "") or "")
    if ":" in expected and not expected.startswith("http."):
        fam = expected.split(":", 1)[0]
    elif "." in expected:
        fam = expected.split(".", 1)[0]
    else:
        fam = "probe" if expected in ("open", "closed") else None
    allowed = {fam} if fam in ("http", "probe", "tls", "dns", "file") else set()
    if fam == "probe":
        # TLS 握手成功同样证明端口开放；但不能拿 tls 结果冒充 HTTP 观察。
        allowed.add("tls")
    out = []
    seen: set[tuple] = set()
    for c in clues:
        if (c.target or "") != host:
            continue
        if allowed and not any(c.kind.startswith(x + ".") for x in allowed):
            continue
        path = getattr(a, "path", None)
        if path is not None and c.path != path:
            continue
        # probe 验收写 host:port 时，线索 key 必须是同一个 host:port
        tgt = getattr(a, "target", "")
        if expected.startswith("probe.") and ":" in tgt and c.key != tgt:
            continue
        key = (c.kind, c.key, c.path)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _observed_text(c) -> str:
    if c.kind.startswith("http."):
        bit = f"{c.kind} @ {(c.path or c.key)}"
        meta = c.meta or {}
        extras = []
        if meta.get("content_length") is not None:
            extras.append(f"len={meta['content_length']}")
        if meta.get("location"):
            extras.append(f"Location={meta['location']}")
        if meta.get("content_type"):
            extras.append(f"type={meta['content_type']}")
        if meta.get("body_sha256"):
            extras.append(f"sha={meta['body_sha256']}")
        return bit + ("（" + " · ".join(extras) + "）" if extras else "")
    if c.kind.startswith("probe."):
        return f"{c.kind} @ {c.key}"
    if c.kind.startswith("tls."):
        return f"{c.kind} @ {c.key}"
    if c.kind.startswith("dns."):
        return f"{c.kind} @ {c.key}={c.value}"
    return f"{c.kind} {c.key}={c.value}"


def _verdict_for(a, candidates) -> str:
    expected = str(getattr(a, "evidence", "") or "").strip()
    if not candidates:
        return _NOT_OBSERVED
    kinds = [c.kind for c in candidates]
    # 覆盖度判据：族:条数
    import re

    m = re.fullmatch(r"(probe|http|shell|tls|dns):(\d+)", expected)
    if m:
        fam, n = m.group(1), int(m.group(2))
        have = len({c.key for c in candidates if c.kind.startswith(fam + ".")})
        return _VERIFIED if have >= n else _NOT_OBSERVED
    if expected in ("open", "closed"):
        expected = "probe." + expected
    if expected == "http.meta":
        return (
            _VERIFIED
            if any(c.kind.startswith("http.") and c.meta.get("headers") for c in candidates)
            else _NOT_OBSERVED
        )
    if expected.startswith(("probe.", "http.", "tls.", "dns.", "file.")):
        exact = [k for k in kinds if k == expected]
        family = expected.split(".", 1)[0]
        same_family = [k for k in kinds if k.startswith(family + ".")]
        if exact:
            return _VERIFIED
        # TLS 握手成功本身就是“端口开放”的证据；对 probe.closed 是直接冲突。
        if expected.startswith("probe.") and any(k.startswith("tls.") for k in kinds):
            return _CONFLICT if expected == "probe.closed" else _VERIFIED
        if same_family:
            # 观察到同族但值不符：不是“达成”，是验收合同与事实冲突
            return _CONFLICT
        # http.meta 例外已在上方处理；http.error 被 http.status 命中走同族冲突
        return _NOT_OBSERVED
    # 旧 key=value / 自定义词：任一候选包含即算观察
    if any(expected in (c.kind, c.key, str(c.value)) for c in candidates):
        return _VERIFIED
    return _NOT_OBSERVED


def _asset_lines(state: MissionState) -> list[str]:
    """从线索推导资产与暴露面（确定性，排序输出）。

    客户报告的「资产视图」比漏洞列表更常被先看：开放了什么、跑了什么
    Web、TLS 什么样。全部来自 probe/http/dns/tls 线索，不编造。"""
    ports = sorted({c.key for c in state.clues if c.kind == "probe.open"})
    closed = sorted({c.key for c in state.clues if c.kind == "probe.closed"})
    endpoints = sorted(
        {(c.key, c.kind) for c in state.clues if c.kind.startswith("http.status")}
    )
    ips: set[str] = set()
    for c in state.clues:
        if c.kind == "dns.result" and isinstance(c.value, dict):
            for ip in c.value.get("ips") or []:
                ips.add(str(ip))
    tls = next((c for c in state.clues if c.kind == "tls.cert"), None)
    lines: list[str] = []
    if ports:
        lines.append("- 开放端口：" + "、".join(ports))
    if closed:
        shown = "、".join(closed[:6]) + ("等" if len(closed) > 6 else "")
        lines.append(f"- 已确认关闭：{shown}（{len(closed)} 个）")
    if ips:
        lines.append("- DNS 解析：" + "、".join(sorted(ips)))
    if endpoints:
        shown = [f"{k} → {kind.rsplit('.', 1)[1]}" for k, kind in endpoints[:10]]
        more = len(endpoints) - len(shown)
        lines.append(
            "- Web 端点：" + "；".join(shown) + (f"（另有 {more} 条）" if more > 0 else "")
        )
    if tls is not None and isinstance(tls.value, dict):
        v = tls.value
        lines.append(
            f"- TLS：{tls.key} {v.get('handshake', '?')} · {v.get('cipher', '?')}"
            f" · verified={v.get('verified', '?')}"
            + (f" · {v.get('days_left', '?')} 天后到期" if v.get("days_left") else "")
        )
    return lines


def _verification_rows(state: MissionState):
    rows = []
    for a in state.spec.acceptance:
        cands = _matching_clues(a, state.clues)
        verdict = _verdict_for(a, cands)
        observed = "；".join(_observed_text(c) for c in cands[:3]) if cands else "-"
        ev = next((c.evidence_id for c in cands if c.evidence_id), None) or "-"
        conf = {
            _VERIFIED: "高",
            _CONFLICT: "中（需复核）",
            _NOT_OBSERVED: "无",
        }.get(verdict, "低")
        rows.append(
            {
                "id": a.id,
                "item": a.item,
                "expected": a.evidence,
                "observed": observed,
                "verdict": verdict,
                "evidence": ev,
                "confidence": conf,
            }
        )
    return rows


def _report_status(state: MissionState, rows) -> tuple[str, list[str]]:
    if not state.tasks:
        return "NOT_EXECUTED", ["任务图为空：规划/引导失败，未执行任何动作"]
    if state.finish_reason == "failed":
        return "FAILED", ["mission 以 failed 结束，不是一次正常扫描"]
    conflicts = [r["id"] for r in rows if r["verdict"] == _CONFLICT]
    if conflicts:
        return (
            "PARTIAL / CONTRACT_CONFLICT",
            [f"验收预期与证据冲突：{'、'.join(conflicts)}"],
        )
    unmet = [r["id"] for r in rows if r["verdict"] != _VERIFIED]
    failed = [t.id for t in state.tasks.values() if t.state in ("failed", "blocked")]
    pending = [t.id for t in state.tasks.values() if t.state == "pending"]
    if unmet or failed or pending:
        return "PARTIAL", []
    return "COMPLETED", []


def _observations(state: MissionState, view: StoreView) -> list[str]:
    """从 worker finish summary 里回收“已排除/观察”结论。

    这些结论以前只躺在 decision.made 里，报告只写“发现 0”。catch-all
    判定、软404、路径同构这类“不是漏洞但必须交代”的结论，应进入报告。
    """
    out: list[str] = []
    keywords = (
        "catch-all",
        "catchall",
        "已排除",
        "不构成",
        "误报",
        "未暴露",
        "软404",
        "软 404",
        "同构",
        "非暴露",
    )
    seen: set[str] = set()
    try:
        for ev in view.events():
            if ev.type != "decision.made":
                continue
            if ev.payload.get("kind") != "finish":
                continue
            summary = str(ev.payload.get("summary") or "")
            low = summary.lower()
            if not any(k.lower() in low for k in keywords):
                continue
            body = " ".join(summary.split())
            if body in seen:
                continue
            seen.add(body)
            out.append(f"OBS-{len(out) + 1}（{ev.payload.get('task_id', '?')}）：{body[:600]}")
    except Exception:
        pass
    # TLS 证书是信息性观察，不是漏洞；有证据就交代
    for c in state.clues:
        if c.kind == "tls.cert" and isinstance(c.value, dict):
            out.append(
                f"INFO-TLS（{c.source_task or '-'}）：{c.key} "
                f"{c.value.get('handshake', '?')} · "
                f"{c.value.get('cipher', '?')} · "
                f"verified={c.value.get('verified', '?')} · "
                f"days_left={c.value.get('days_left', '?')}"
            )
            break
    return out


def _execution_quality(state: MissionState, view: StoreView) -> list[str]:
    counts = {}
    for t in state.tasks.values():
        counts[t.state] = counts.get(t.state, 0) + 1
    restarts = 0
    model_errors = 0
    worker_returns = 0
    plan_modes: list[str] = []
    try:
        for ev in view.events():
            if ev.type == "restart.decided":
                restarts += 1
            elif ev.type == "plan.created":
                plan_modes.append(str(ev.payload.get("mode") or "planner"))
            elif ev.type == "worker.returned":
                worker_returns += 1
                if (ev.payload.get("failure") or {}).get("cls") == "Model":
                    model_errors += 1
    except Exception:
        pass
    lines = [
        f"- 结算：{state.step} 步 · {state.spent_tokens} tokens",
        "- 任务：" + ("、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "无"),
        f"- 派发/重试：worker 返回 {worker_returns} 次，restart {restarts} 次",
        f"- 质量信号：virtual_success {int(state.meta.get('virtual_success_count', 0))} 次，"
        f"Model 解析失败 {model_errors} 次",
    ]
    if plan_modes:
        lines.append("- 计划模式：" + "、".join(plan_modes))
    pr = state.meta.get("plateau_reason")
    if state.finish_reason:
        lines.append(
            f"- 终态：{state.finish_reason}" + (f"（{pr}）" if pr else "")
        )
    return lines


def _next_actions(rows) -> list[str]:
    acts: list[str] = []
    for r in rows:
        if r["verdict"] == _VERIFIED:
            continue
        exp = r["expected"]
        if r["verdict"] == _CONFLICT:
            acts.append(
                f"复核 {r['id']}：验收预期 {exp} 与实际 {r['observed']} 冲突，"
                "确认目标真实状态后修订验收或补采。"
            )
        elif exp.startswith("probe."):
            acts.append(f"补充 {r['id']}：对 {r['item']} 做一次 probe 并记录实际端口状态。")
        elif exp.startswith("http."):
            acts.append(
                f"补充 {r['id']}：重新请求目标路径，采集状态码/响应头/长度，"
                "不要把 200 当成预期值已验证。"
            )
        elif exp.startswith("tls."):
            acts.append(f"补充 {r['id']}：对目标端口做一次 TLS 握手与证书采集。")
        elif exp.startswith("dns."):
            acts.append(f"补充 {r['id']}：用 dns 工具解析目标并记录 A/AAAA。")
        else:
            acts.append(f"补充 {r['id']}：为 {r['item']} 采集可判定证据。")
    if not acts:
        acts.append("当前验收项均有证据；如无 confirmed finding，按“无发现”交付并附覆盖说明。")
    return acts[:8]


def render_report(state: MissionState, view: StoreView) -> str:
    lines: list[str] = []
    w = lines.append
    rows = _verification_rows(state)
    status, status_notes = _report_status(state, rows)
    w("# secpt 渗透测试报告")
    w("")
    w(f"> STATUS: {status}")
    for note in status_notes:
        w(f"> {note}")
    if status != "COMPLETED":
        w("> 本报告不是一次“无发现即无风险”的干净扫描；请先阅读验收核对与未验证项。")
    w("")
    w("## 结论摘要")
    w("")
    verified = [r["id"] for r in rows if r["verdict"] == _VERIFIED]
    conflicts = [r["id"] for r in rows if r["verdict"] == _CONFLICT]
    missing = [r["id"] for r in rows if r["verdict"] == _NOT_OBSERVED]
    if verified:
        w(f"- 已验证：{'、'.join(verified)}")
    if conflicts:
        w(f"- 验收预期与证据冲突：{'、'.join(conflicts)}（不是单纯的“未达成”）")
    if missing:
        w(f"- 未观察/未验证：{'、'.join(missing)}")
    w(f"- 发现：{len(state.findings)} 条 finding（详见发现与附录）")
    w(
        "- 阅读提示：本报告的“已验证”指实际观察与验收预期一致；"
        "“冲突”指实际观察与预期不一致，需人工复核验收或目标状态。"
    )
    w("")
    w("## 概述")
    w("")
    w(f"- 目标：{'、'.join(state.spec.targets)}")
    reason = state.finish_reason or "未完成"
    pr = state.meta.get("plateau_reason")
    w(f"- 终止原因：{reason}" + (f"（{pr}）" if pr else ""))
    satisfied = _satisfied(state)
    w(f"- 验收：{len(satisfied)}/{len(state.spec.acceptance)} 项达成")
    if len(verified) != len(satisfied):
        w(
            f"- 严格验证：{len(verified)}/{len(state.spec.acceptance)} 项"
            "与验收预期一致（其余为同族观察或冲突，见矩阵）"
        )
    counts: dict[str, int] = {}
    for t in state.tasks.values():
        counts[t.state] = counts.get(t.state, 0) + 1
    w(
        f"- 任务：{len(state.tasks)}（"
        + "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
        + "）"
    )
    # 计数口径必须与正文（confirmed 过滤 / 附录）一致：按合并后 findings 逐条
    # 反查裁决，而不是数 adjudications 条数——同 claim 多次提案会被
    # merge_finding 折叠成一条，按裁决条数计数会让「发现 1」配出「suspected 2」
    # 的自相矛盾（真跑 do-4a1e630e 实例）。
    verdicts = [
        state.adjudications.get(_fid(f), _UNADJUDICATED_SENTINEL).verdict
        for f in state.findings
    ]
    w(
        f"- 发现：{len(state.findings)}（confirmed {verdicts.count('confirmed')}、"
        f"suspected {verdicts.count('suspected')}、"
        f"false_positive {verdicts.count('false_positive')}）"
    )
    audit = audit_refs(state, view.root)
    w(
        f"- 证据审计：引用 {audit['refs']} 条，可核验 {audit['verified']}，"
        f"断链 {sum(len(b['refs']) for b in audit['broken'])}"
    )
    if audit["confirmed_broken"]:
        # confirmed 却含断链引用 = 账链漂移信号：裁决口径（≥1 条可核验）
        # 允许其他引用损坏，审计显式亮出（M3 不吞信号）
        w(
            "  - 注意："
            + "、".join(audit["confirmed_broken"])
            + " 为 confirmed 但含断链引用（详见 mission.audit 事件）"
        )
    w(f"- 结算：step {state.step}，tokens {state.spent_tokens}")
    w("")
    assets = _asset_lines(state)
    w("## 资产与暴露面")
    w("")
    if assets:
        for line in assets:
            w(line)
    else:
        w("- 无（未采集到端口/Web/TLS/DNS 线索）")
    w("")
    w("## 验收核对矩阵")
    w("")
    w("| 验收项 | 预期 | 实际观察 | 判定 | 证据 | 置信度 |")
    w("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        ev = r["evidence"]
        ev = f"`{ev[:20]}`" if ev.startswith("ev-") else ev
        observed = str(r["observed"]).replace("|", "\\|")
        item = str(r["item"]).replace("|", "\\|")
        w(
            f"| {r['id']} {item} | {r['expected']} | {observed} | "
            f"{r['verdict']} | {ev} | {r['confidence']} |"
        )
    w("")
    w("## 已达成验收项（严格验证）")
    w("")
    verified_ids = {r["id"] for r in rows if r["verdict"] == _VERIFIED}
    strict = [a for a in state.spec.acceptance if a.id in verified_ids]
    if strict:
        for a in strict:
            w(f"- {a.id}：{a.item}")
    else:
        w("- 无（同族观察不等于预期值已验证；见验收核对矩阵）")
    w("")
    w("## 发现（confirmed）")
    w("")
    confirmed = [
        f
        for f in _sorted_findings(state)
        if state.adjudications.get(_fid(f), _UNADJUDICATED_SENTINEL).verdict
        == "confirmed"
    ]
    if confirmed:
        for f in confirmed:
            _render_finding(w, state, view, f)
    else:
        w("- 无已确认发现")
    w("")
    w("## 未达成项与原因")
    w("")
    unmet = [a for a in state.spec.acceptance if not _item_met(state, a)]
    if unmet:
        for a in unmet:
            w(f"- {a.id}：{a.item}")
            row = next((r for r in rows if r["id"] == a.id), None)
            if row is not None and row["verdict"] == _CONFLICT:
                w(
                    f"  - 原因：验收预期 {a.evidence} 与实际观察 "
                    f"{row['observed']} 冲突（不是没采到，是预期写错/需人工复核）"
                )
            elif row is not None and row["observed"] != "-":
                w(
                    f"  - 原因：已观察到 {row['observed']}，但未达到验收预期 "
                    f"{a.evidence}（见验收核对矩阵）"
                )
            else:
                w(f"  - 原因：无任何线索命中判据 evidence={a.evidence}")
            related = [t for t in state.tasks.values() if not (t.state == "done")]
            if related:
                desc = "；".join(
                    f"{t.id}（{t.state}"
                    + (f"，原因 {t.block_reason}" if t.block_reason else "")
                    + "）"
                    for t in sorted(related, key=lambda x: x.id)
                )
                w(f"  - 未完成任务：{desc}")
    else:
        w("- 无（全部达成）")
    w("")
    observations = _observations(state, view)
    w("## 已排除 / 观察")
    w("")
    if observations:
        for line in observations:
            w(f"- {line}")
    else:
        w("- 无（注意：0 finding 不等于无风险；未验证项见上）")
    w("")
    w("## 执行质量（内部）")
    w("")
    for line in _execution_quality(state, view):
        w(line)
    w("")
    w("## 下一步")
    w("")
    for i, action in enumerate(_next_actions(rows), 1):
        w(f"{i}. {action}")
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
    if getattr(adj, "unlock", ""):
        w(f"- 补证方向：{adj.unlock}")
    w("- 证据链：")
    for ref in f.evidence_refs:
        w(f"  - {_ref_line(view, ref)}")
    if not f.evidence_refs:
        w("  - （无证据引用）")
    w(f"- 复现步骤：{f.reproduce}")
    w(f"- 证伪条件：{'；'.join(f.falsify_if) if f.falsify_if else '未声明'}")
    w("")


def _render_appendix(w, state, view) -> None:
    for verdict, title in (
        ("suspected", "附录 A：疑似发现（suspected）"),
        ("false_positive", "附录 B：已排除发现（false_positive）"),
    ):
        w(f"## {title}")
        w("")
        fs = [
            f
            for f in _sorted_findings(state)
            if state.adjudications.get(_fid(f), _UNADJUDICATED_SENTINEL).verdict
            == verdict
        ]
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
