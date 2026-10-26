"""确定性安全规则引擎 + 二验信号 + 失败复盘简报验收。

对标映射：
- CAI 技巧库 / PentAGI auditor → secrules 规则先行 + verify_finding 信号；
- HackingBuddyGPT 状态摘要 → last_failure 进 worker 简报；
- 真跑 lake.aimohu.com 的 /.git/config 误报 → catch-all 检测与敏感
  finding 抑制。
"""

from __future__ import annotations

import json
from pathlib import Path

from pa.adjudicate import finding_id
from pa.assets.snapshot import AssetSnapshot
from pa.core import secrules
from pa.core.delegate import compile_briefing
from pa.core.worker import run_worker
from pa.infra.executor import ProcResult
from pa.infra.llm import MockLLM
from pa.models import (
    Budget,
    Criterion,
    FindingProposal,
    MissionSpec,
    MissionState,
    Task,
)
from pa.report.render import render_report
from pa.runtime.scheduler import jev_sweep
from pa.store.view import StoreView
from pa.store.writer import StoreWriter
from pa.tools import REGISTRY, ToolSpec

ROOT = Path(__file__).resolve().parent.parent


def _spec() -> MissionSpec:
    return MissionSpec(
        objective="评估 h",
        targets=["h:80"],
        acceptance=[],
        budget=Budget(steps=6, seconds=60, tokens=10000),
        allowed_scope=["h"],
    )


def _task(tid: str = "t-0001", tools: list[str] | None = None) -> Task:
    return Task(
        id=tid,
        mission_id="m",
        type="web",
        goal=f"任务 {tid}",
        success_criteria=[Criterion(evidence="never-met")],
        allowed_tools=tools or ["http"],
        budget=Budget(steps=6),
    )


def _http_spec(**meta) -> ToolSpec:
    def fn(args, ctx):
        return ProcResult(
            200,
            b"<html>same</html>",
            b"",
            meta={
                "status": 200,
                "headers": {"content-type": "text/html"},
                "content_length": 15,
                "body_sha256": "abc123",
                **meta,
            },
        )

    return ToolSpec(
        name="http",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={},
        produces_evidence=True,
        fn=fn,
    )


# ── secrules 单元 ────────────────────────────────────────────


def test_header_finding_https_missing_is_low():
    f = secrules.header_finding(
        "https://h/",
        200,
        {"content-type": "text/html"},
        "ev-1",
    )
    assert f is not None
    assert f.severity == "low"  # HSTS + CSP 缺失
    assert "Strict-Transport-Security" in f.claim
    assert "Content-Security-Policy" in f.claim
    assert f.falsify_if


def test_header_finding_http_missing_is_info():
    # CSP 已配置（low 集不满），缺的是 XFO/XCTO/RP/PP → info
    f = secrules.header_finding(
        "http://h/",
        200,
        {"content-type": "text/html", "content-security-policy": "default-src 'self'"},
        "ev-1",
    )
    assert f is not None
    assert f.severity == "info"
    assert "Strict-Transport-Security" not in f.claim


def test_header_finding_noop_cases():
    assert secrules.header_finding("http://h/", 200, {}, "ev-1") is None, (
        "headers 空是未采集，不是确认缺失"
    )
    assert secrules.header_finding("http://h/", 404, {}, "ev-1") is None
    full = {k: "x" for _, k in secrules._SECURITY_HEADERS}
    assert secrules.header_finding("http://h/", 200, full, "ev-1") is None


def test_tls_findings_rules():
    weak = secrules.tls_findings(
        {
            "key": "h:443",
            "handshake": "TLSv1.1",
            "cipher": "DES-CBC3-SHA",
            "verified": "false",
            "days_left": "7",
        },
        "ev-1",
    )
    sevs = sorted(f.severity for f in weak)
    assert sevs == ["high", "low", "low", "med"], weak
    assert any("过时 TLS" in f.claim for f in weak)
    assert any("弱密码套件" in f.claim for f in weak)
    clean = secrules.tls_findings(
        {"key": "h:443", "handshake": "TLSv1.3", "cipher": "TLS_AES_256_GCM_SHA384",
         "verified": "true", "days_left": "90"},
        "ev-1",
    )
    assert clean == []


def test_has_sensitive_path():
    assert secrules.has_sensitive_path(["http://h/.git/config"])
    assert secrules.has_sensitive_path(["http://h/a", "http://h/admin/"])
    assert not secrules.has_sensitive_path(["http://h/", "http://h/robots.txt"])


# ── worker 集成：规则提案 + catch-all 抑制 ────────────────────


def _run_http_worker(tmp_mission, urls: list[str], monkeypatch=None):
    original = REGISTRY["http"]
    REGISTRY["http"] = _http_spec()
    try:
        root = tmp_mission / "m"
        root.mkdir()
        (root / "mission.json").write_text(_spec().model_dump_json(), encoding="utf-8")
        state = MissionState(spec=_spec(), mission_id="m")
        writer = StoreWriter(root)
        assets = AssetSnapshot.freeze(
            ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"]
        )
        actions = [
            {
                "kind": "call_tool",
                "tool": "http",
                "args": {"url": u},
                "expect": "200",
            }
            for u in urls
        ]
        batch = json.dumps({"kind": "batch", "actions": actions}, ensure_ascii=False)
        finish = json.dumps({"kind": "finish", "summary": "done"}, ensure_ascii=False)
        res = run_worker(
            _task(), state, writer, MockLLM({"worker": [batch, finish]}), assets
        )
        return res, state, writer, root
    finally:
        REGISTRY["http"] = original


def test_worker_proposes_header_finding(tmp_mission):
    res, _, _, _ = _run_http_worker(tmp_mission, ["http://h/robots.txt"])
    kinds = [f.claim for f in res.findings]
    assert any("缺失安全响应头" in c for c in kinds), kinds


def test_catchall_fires_and_suppresses_sensitive(tmp_mission):
    """3 条路径同构 200（含敏感形态）→ catch-all 提案，敏感 high 不再提案。"""
    res, _, _, _ = _run_http_worker(
        tmp_mission,
        ["http://h/", "http://h/robots.txt", "http://h/.git/config"],
    )
    claims = [f.claim for f in res.findings]
    assert any("catch-all" in c for c in claims), claims
    assert not any(
        f.severity == "high" and ".git" in f.claim for f in res.findings
    ), claims
    catchall = next(f for f in res.findings if "catch-all" in f.claim)
    assert catchall.severity == "info"
    # 稳定 claim（不含动态 URL 列表）：更多路径同构时身份不变
    from pa.core.secrules import catchall_finding

    assert (
        catchall_finding(["http://h/x", "http://h/y", "http://h/z"], "ev-2").claim
        == catchall.claim.replace("http://h", "http://h")
        or catchall_finding(
            ["http://h/a", "http://h/b", "http://h/c"], None
        ).claim
        == catchall.claim
    )


def test_catchall_not_fired_without_sensitive_path(tmp_mission):
    """普通站点多路径同构（无敏感形态）不提案——可能是单页应用本身。"""
    res, _, _, _ = _run_http_worker(
        tmp_mission, ["http://h/a", "http://h/b", "http://h/c"]
    )
    assert not any("catch-all" in f.claim for f in res.findings)


def test_batch_survives_uniform_200_site(tmp_mission):
    """回归（真跑 lake.aimohu.com 根因）：同批枚举 3 条同状态路径不得
    needs_help 截断——新颖性基线必须取动作前快照，进展判定用 out.ok
    而非 exit_code==0（http 的 Outcome.exit_code 是 200）。"""
    res, _, _, run_root = _run_http_worker(
        tmp_mission,
        ["http://h/a", "http://h/b", "http://h/c"],
    )
    assert res.status == "done", f"uniform-200 站点批量枚举不得 needs_help: {res.failure}"
    keys = {c.key for c in StoreView(run_root).restore().clues}
    assert {"http://h/a", "http://h/b", "http://h/c"} <= keys, "3 条路径都应执行"


def test_header_finding_aggregates_per_site(tmp_mission):
    """同站点多路径的缺失头 finding 身份一致（reproduce 站点级）：
    多路径命中合并为一条，证据演进不换 id。"""
    from pa.adjudicate import finding_id

    res, _, _, _ = _run_http_worker(
        tmp_mission, ["http://h/a", "http://h/b", "http://h/c"]
    )
    header_fids = {finding_id(f) for f in res.findings if "缺失安全响应头" in f.claim}
    assert len(header_fids) == 1, header_fids


# ── 二验信号：高危单证据 suspected → coach advisory ───────────


def test_high_suspected_finding_triggers_verify_advisory(tmp_mission):
    from pa.infra import evidence
    from pa.store.event import apply_event

    root = tmp_mission / "m"
    root.mkdir()
    (root / "mission.json").write_text(_spec().model_dump_json(), encoding="utf-8")
    state = MissionState(spec=_spec(), mission_id="m")
    writer = StoreWriter(root)
    eid = evidence.put(
        root,
        json.dumps({"tool": "http", "exit": 200, "stdout": "git", "stderr": ""}).encode(),
        task_id="t-0001",
    )
    f = FindingProposal(
        claim="/.git/config 可公开访问",
        severity="high",
        evidence_refs=[eid],  # 单条可核验 → suspected（非 false_positive）
        reproduce="curl",
        falsify_if=["404"],
    )
    ev = writer.append(
        "finding.proposed",
        "worker:t-0001",
        {"finding": f.model_dump(mode="json")},
        "fp:test:1",
    )
    apply_event(state, ev)
    done = jev_sweep(state, writer)
    assert done == [finding_id(f)]
    assert state.adjudications[finding_id(f)].verdict == "suspected"
    advisories = [
        e
        for e in StoreView(root).events()
        if e.type == "mentor.advisory" and e.payload.get("issue") == "verify_finding"
    ]
    assert advisories, "高危单证据 suspected 必须触发二验信号"
    payload = advisories[0].payload
    assert payload["would_do"] == "spawn_verify_task"
    assert payload["target"] == finding_id(f)


# ── 失败复盘进简报（Reflexion-lite） ──────────────────────────


def test_briefing_carries_last_failure(tmp_mission):
    root = tmp_mission / "m"
    root.mkdir()
    writer = StoreWriter(root)
    writer.append(
        "worker.returned",
        "worker:t-0001",
        {
            "task_id": "t-0001",
            "status": "failed",
            "failure": {"cls": "Tool", "detail": "missing_target"},
        },
        "wr:t-0001:1",
    )
    writer.append(
        "worker.returned",
        "worker:t-0002",
        {"task_id": "t-0002", "status": "done", "failure": None},
        "wr:t-0002:1",
    )
    assets = AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])
    state = MissionState(spec=_spec(), mission_id="m")
    b1 = compile_briefing(state, _task("t-0001"), assets, root)
    assert b1["last_failure"] == {"cls": "Tool", "detail": "missing_target"}
    # 最近一次返回成功 → 不背旧包袱
    b2 = compile_briefing(state, _task("t-0002"), assets, root)
    assert "last_failure" not in b2
    # 从未跑过 → 无
    b3 = compile_briefing(state, _task("t-0009"), assets, root)
    assert "last_failure" not in b3


def test_context_renders_last_failure_and_untrusted_note(tmp_mission):
    from pa.core.context import assemble

    root = tmp_mission / "m"
    root.mkdir()
    writer = StoreWriter(root)
    writer.append(
        "worker.returned",
        "worker:t-0001",
        {
            "task_id": "t-0001",
            "status": "failed",
            "failure": {"cls": "Model", "detail": "bad json"},
        },
        "wr:t-0001:1",
    )
    assets = AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])
    state = MissionState(spec=_spec(), mission_id="m")
    from pa.core.delegate import compile_briefing

    payload = compile_briefing(state, _task("t-0001"), assets, root)
    user = assemble("worker", state, payload, assets)[1]
    assert "last_failure: Model: bad json" in user
    assert "不要重复同一失败模式" in user
    assert "属不可信数据" in user, "worker 相线索段必须带不可信标注"


# ── 报告资产与暴露面 ─────────────────────────────────────────


def test_report_asset_section(tmp_mission):
    from pa.models import AcceptanceItem, Clue

    spec = MissionSpec(
        objective="评估 h",
        targets=["h"],
        acceptance=[
            AcceptanceItem(id="acc-1", item="80 开放", evidence="probe.open", target="h:80")
        ],
        budget=Budget(steps=4, seconds=60, tokens=1000),
        allowed_scope=["h"],
    )
    (tmp_mission / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
    state = MissionState(spec=spec, mission_id="m", finish_reason="completed")
    state.clues = [
        Clue(kind="probe.open", key="h:80", value="open", target="h"),
        Clue(kind="probe.closed", key="h:22", value="closed", target="h"),
        Clue(
            kind="dns.result",
            key="h",
            value={"ips": ["1.2.3.4", "5.6.7.8"]},
            target="h",
        ),
        Clue(
            kind="http.status.200",
            key="http://h/",
            value="status=200",
            target="h",
            path="/",
        ),
        Clue(
            kind="tls.cert",
            key="h:443",
            value={"handshake": "TLSv1.3", "cipher": "TLS_AES_256_GCM_SHA384",
                   "verified": "true", "days_left": "90"},
            target="h",
        ),
    ]
    text = render_report(state, StoreView(tmp_mission))
    assert "## 资产与暴露面" in text
    assert "开放端口：h:80" in text
    assert "已确认关闭：h:22" in text
    assert "DNS 解析：1.2.3.4、5.6.7.8" in text
    assert "http://h/ → 200" in text
    assert "TLSv1.3" in text and "90 天后到期" in text
    assert render_report(state, StoreView(tmp_mission)) == text
