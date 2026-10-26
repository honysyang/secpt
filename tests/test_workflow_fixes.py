"""工作流修复验收：合同门 / 批次完整执行 / 依赖失败传播 / run 分配 / 报告。"""

from __future__ import annotations

from pa.cli import _next_run_path
from pa.core.planner import _guard_create
from pa.graph.ready import block_dep_failed
from pa.models import (
    AcceptanceItem,
    Budget,
    Clue,
    Criterion,
    MissionSpec,
    MissionState,
    Task,
    TaskDraft,
)
from pa.policy import review
from pa.report.render import render_report
from pa.store.view import StoreView
from pa.store.writer import StoreWriter


def _spec() -> MissionSpec:
    return MissionSpec(
        objective="探测 h",
        targets=["h"],
        acceptance=[
            AcceptanceItem(id="acc-1", item="80 开放", evidence="probe.open", target="h:80"),
            AcceptanceItem(id="acc-2", item="443 关闭", evidence="probe.closed", target="h:443"),
            AcceptanceItem(
                id="acc-3",
                item="根路径 301",
                evidence="http.status.301",
                target="h:80",
                path="/",
            ),
        ],
        budget=Budget(steps=6, seconds=60, tokens=1000),
        allowed_scope=["h"],
    )


def _task(tid="t-0001", *, state="pending", deps=None, attempt=0) -> Task:
    return Task(
        id=tid,
        mission_id="m",
        type="recon",
        goal=f"task {tid}",
        deps=deps or [],
        success_criteria=[Criterion(evidence="probe.open")],
        allowed_tools=["probe"],
        state=state,
        attempt=attempt,
    )


def test_contract_rejects_prose_and_unregistered_criteria():
    bad = TaskDraft(
        type="recon",
        goal="汇总",
        success_criteria=[Criterion(evidence="fs.read")],
        scope_slice={"host": "h"},
        allowed_tools=["fs"],
    )
    prose = TaskDraft(
        type="recon",
        goal="汇总",
        success_criteria=[Criterion(evidence="给出 A 记录或明确失败原因")],
        scope_slice={"host": "h"},
        allowed_tools=["fs"],
    )
    good = TaskDraft(
        type="recon",
        goal="探活",
        success_criteria=[Criterion(evidence="probe.open")],
        scope_slice={"host": "h"},
        allowed_tools=["probe"],
    )
    kept, rejected = review.filter_valid_drafts([bad, prose, good])
    assert [d.goal for d in kept] == ["探活"]
    assert len(rejected) == 2
    assert any(i.code == "bad_criterion" for i in review.review_drafts([bad]))


def test_mixed_draft_keeps_valid_criterion_and_flags_bad_one():
    d = TaskDraft(
        type="web",
        goal="请求 admin",
        success_criteria=[
            Criterion(evidence="http.status.200"),
            Criterion(evidence="给出响应头与长度差异"),
        ],
        scope_slice={"host": "h"},
        allowed_tools=["http"],
    )
    kept, rejected = review.filter_valid_drafts([d])
    assert len(kept) == 1 and [c.evidence for c in kept[0].success_criteria] == [
        "http.status.200"
    ]
    assert rejected and "给出响应头与长度差异" in rejected[0][1]


def test_dep_failure_blocks_downstream(tmp_mission):
    writer = StoreWriter(tmp_mission)
    state = MissionState(spec=_spec(), mission_id="m")
    dep = _task("t-0001", state="failed", attempt=3)
    child = _task("t-0002", deps=["t-0001"])
    state.tasks = {"t-0001": dep, "t-0002": child}
    blocked = block_dep_failed(state, writer, max_attempts=3)
    assert blocked == ["t-0002"]
    assert child.state == "blocked"
    assert child.block_reason == "dep_failed:t-0001"
    # 依赖还会重试时不能提前判死
    dep.attempt = 1
    child2 = _task("t-0003", deps=["t-0001"])
    state.tasks["t-0003"] = child2
    assert block_dep_failed(state, writer, max_attempts=3) == []
    assert child2.state == "pending"


def test_next_run_path_never_reuses_existing_ledger(tmp_path):
    family = tmp_path / "fam"
    assert _next_run_path(family) == family / "runs" / "1"
    (family / "runs" / "1").mkdir(parents=True)
    assert _next_run_path(family) == family / "runs" / "2"
    (family / "runs" / "2").mkdir()
    assert _next_run_path(family) == family / "runs" / "3"
    # 家族根旧裸账本视作 N=1，下一个至少 2
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "events.jsonl").write_text("", encoding="utf-8")
    assert _next_run_path(legacy) == legacy / "runs" / "2"


def test_no_tasks_is_failed_not_plateau():
    from pa.runtime.loop import check_termination

    state = MissionState(spec=_spec(), mission_id="m")
    assert check_termination(state) == "failed"


def test_report_status_matrix_and_observation(tmp_mission):
    spec = _spec()
    state = MissionState(spec=spec, mission_id="m", finish_reason="plateau", step=3)
    state.tasks["t-0001"] = _task("t-0001", state="done")
    state.clues = [
        Clue(kind="probe.open", key="h:80", value="open", target="h"),
        Clue(
            kind="tls.cert",
            key="h:443",
            value={"handshake": "TLSv1.3", "verified": "true"},
            target="h",
        ),
        Clue(
            kind="http.status.200",
            key="http://h/",
            value="status=200",
            target="h",
            path="/",
        ),
    ]
    (tmp_mission / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
    writer = StoreWriter(tmp_mission)
    writer.append(
        "decision.made",
        "worker:t-0001",
        {
            "kind": "finish",
            "task_id": "t-0001",
            "summary": "路径返回 200 但正文同构，属 catch-all，不构成真实暴露",
        },
        "dm:test:finish",
    )
    text = render_report(state, StoreView(tmp_mission))
    assert "> STATUS: PARTIAL / CONTRACT_CONFLICT" in text
    assert "## 验收核对矩阵" in text
    assert "acc-1" in text and "VERIFIED" in text
    assert "CONTRACT_CONFLICT" in text
    assert "严格验证：1/3" in text
    assert "## 已排除 / 观察" in text
    assert "catch-all" in text and "OBS-1" in text
    assert "## 执行质量（内部）" in text
    assert "## 下一步" in text
    assert render_report(state, StoreView(tmp_mission)) == text


def test_finish_findings_become_jev_finding(tmp_mission):
    """worker 收口随附的 findings 落 finding.proposed，JEV 裁决 + unlock。"""
    import json as _json
    from pathlib import Path

    from pa.adjudicate import finding_id
    from pa.assets.snapshot import AssetSnapshot
    from pa.core.worker import run_worker
    from pa.infra.executor import ProcResult
    from pa.infra.llm import MockLLM
    from pa.models import Finish, FinishFinding
    from pa.runtime.scheduler import jev_sweep
    from pa.tools import REGISTRY, ToolSpec

    assets = AssetSnapshot.freeze(
        Path(__file__).resolve().parent.parent / "pa" / "core" / "prompts",
        [Path(__file__).resolve().parent.parent / "assets" / "skills"],
    )
    original = REGISTRY["probe"]

    def fake_probe(args, ctx):
        return ProcResult(0, b"open\n", b"")

    REGISTRY["probe"] = ToolSpec(
        name="probe",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={},
        produces_evidence=True,
        fn=fake_probe,
    )
    try:
        root = tmp_mission / "m"
        root.mkdir()
        spec = _spec()
        (root / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
        state = MissionState(spec=spec, mission_id="m")
        task = _task("t-0001")
        # 判据设为永不满足，迫使 worker 走完 probe → finish 两个 ReAct 步
        task.success_criteria = [Criterion(evidence="never-met")]
        writer = StoreWriter(root)
        dec = Finish(
            summary="80 开放，admin 是 catch-all 不是暴露",
            satisfied=[],
            findings=[
                FinishFinding(
                    claim="80/tcp 对外开放",
                    severity="info",
                    reproduce="probe lake:80",
                    falsify_if=["端口被防火墙拦截"],
                ),
                FinishFinding(
                    claim="/admin/ 是 SPA catch-all，不构成暴露",
                    severity="info",
                ),
            ],
        )
        # 单决策路径：call_tool 一轮（产证据）→ finish 一轮（提案）
        probe_dec = _json.dumps(
            {
                "kind": "call_tool",
                "tool": "probe",
                "args": {"target": "h", "port": 80},
                "expect": "open",
            }
        )
        finish_dec = dec.model_dump_json()
        res = run_worker(
            task, state, writer, MockLLM({"worker": [probe_dec, finish_dec]}), assets
        )
        assert res.status == "done"
        assert len(res.findings) == 2, "有证据时随附提案都应落账"
        assert all(f.evidence_refs for f in res.findings), "证据由引擎绑定"
        assert all(f.origin == ["t-0001"] for f in res.findings)
        # 稳定身份：claim 不同 → 不同 fid
        fids = {finding_id(f) for f in res.findings}
        assert len(fids) == 2

        # JEV sweep 裁决 + unlock 提示
        done = jev_sweep(state, writer)
        assert len(done) == 2
        for f in res.findings:
            adj = state.adjudications[finding_id(f)]
            assert adj.verdict == "suspected", "单引用证据 → suspected"
            assert adj.unlock, "suspected 必须带补证方向"
    finally:
        REGISTRY["probe"] = original


def test_finish_findings_skipped_without_evidence(tmp_mission):
    """本任务没有产出证据时不提案：0 引用提案只会污染台账。"""
    from pathlib import Path

    from pa.assets.snapshot import AssetSnapshot
    from pa.core.worker import run_worker
    from pa.infra.llm import MockLLM
    from pa.models import Budget, Finish, FinishFinding, Task

    assets = AssetSnapshot.freeze(
        Path(__file__).resolve().parent.parent / "pa" / "core" / "prompts",
        [Path(__file__).resolve().parent.parent / "assets" / "skills"],
    )
    root = tmp_mission / "m"
    root.mkdir()
    (root / "mission.json").write_text(_spec().model_dump_json(), encoding="utf-8")
    state = MissionState(spec=_spec(), mission_id="m")
    task = Task(
        id="t-0002",
        mission_id="m",
        type="recon",
        goal="汇总（无网络工具）",
        success_criteria=[Criterion(evidence="probe.open")],
        allowed_tools=["fs"],
        budget=Budget(steps=2),
    )
    writer = StoreWriter(root)
    finish = Finish(
        summary="纯汇总",
        findings=[FinishFinding(claim="某暴露", severity="high")],
    )
    res = run_worker(
        task, state, writer, MockLLM({"worker": [finish.model_dump_json()]}), assets
    )
    assert res.status == "done"
    assert res.findings == [], "无证据时不得提案"


def test_stable_finding_id_across_evidence_evolution():
    """身份只由 claim/reproduce/asset_id 决定；evidence/severity 演进不换 id。"""
    from pa.adjudicate import finding_id, proposal_key
    from pa.models import FindingProposal

    base = FindingProposal(
        claim="/admin/ 是 catch-all",
        severity="info",
        evidence_refs=["ev-a"],
        reproduce="GET /admin/",
        falsify_if=["返回登录页"],
    )
    evolved = base.model_copy(
        update={"evidence_refs": ["ev-a", "ev-b"], "severity": "low"}
    )
    assert finding_id(base) == finding_id(evolved)
    assert proposal_key(base) != proposal_key(evolved), "内容演化算新提案事件"


def test_guard_create_dedupes_normalized_goals(tmp_mission):
    """「汇总结论。」与「汇总结论」是同一件事；superseded 的 goal 可重建。"""
    from pa.graph.task import create_task
    from pa.models import TaskDraft

    writer = StoreWriter(tmp_mission)
    state = MissionState(spec=_spec(), mission_id="m")
    create_task(
        state,
        writer,
        TaskDraft(
            type="recon",
            goal="汇总 lake 攻击面结论",
            success_criteria=[Criterion(evidence="probe.open")],
            scope_slice={"host": "lake"},
            allowed_tools=["probe"],
        ),
    )
    dup = TaskDraft(
        type="recon",
        goal="汇总 lake 攻击面结论！",
        success_criteria=[Criterion(evidence="probe.open")],
        scope_slice={"host": "lake"},
        allowed_tools=["probe"],
    )
    fresh = TaskDraft(
        type="recon",
        goal="探测 443 端口",
        success_criteria=[Criterion(evidence="probe.open")],
        scope_slice={"host": "lake"},
        allowed_tools=["probe"],
    )
    made, dropped = _guard_create(state, writer, [dup, fresh])
    assert [t.goal for t in made] == ["探测 443 端口"]
    assert dropped == 1

    # superseded 的 goal 允许重建（替代任务场景）
    victim = next(iter(state.tasks.values()))
    victim.state = "superseded"
    made2, dropped2 = _guard_create(state, writer, [dup])
    assert [t.goal for t in made2] == ["汇总 lake 攻击面结论！"]
    assert dropped2 == 0


def test_guard_create_respects_mission_task_cap(tmp_mission):
    from pa.core.planner import _guard_create
    from pa.graph.task import create_task
    from pa.models import TaskDraft
    from pa.policy import thresholds

    writer = StoreWriter(tmp_mission)
    state = MissionState(spec=_spec(), mission_id="m")
    cap = int(thresholds.THRESHOLDS["mission_task_cap"])
    for i in range(cap):
        create_task(
            state,
            writer,
            TaskDraft(
                type="recon",
                goal=f"任务 {i}",
                success_criteria=[Criterion(evidence="probe.open")],
                scope_slice={"host": "lake"},
                allowed_tools=["probe"],
            ),
        )
    draft = TaskDraft(
        type="recon",
        goal="超限任务",
        success_criteria=[Criterion(evidence="probe.open")],
        scope_slice={"host": "lake"},
        allowed_tools=["probe"],
    )
    made, dropped = _guard_create(state, writer, [draft])
    assert made == [] and dropped == 1, "到达上限后 create 整批拒绝"
    assert len(state.tasks) == cap


def test_log_render_with_finish_thought(tmp_mission):
    """回归：action 流里夹着 decision.made(finish) 时，finished 配对不得
    把思考节点当动作（legacy lake 账本实测 KeyError: 'fp'）。"""
    import json as _json

    from pa.report.log import render_log

    root = tmp_mission / "m"
    root.mkdir()
    spec = _spec()
    (root / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
    lines = [
        _json.dumps(
            {
                "seq": 1,
                "mission_id": "m",
                "ts": "2026-10-26T00:00:00Z",
                "type": "mission.created",
                "actor": "engine",
                "payload": {"objective": "o", "targets": ["h"]},
                "idem_key": "mc:m",
            },
            ensure_ascii=False,
        ),
        _json.dumps(
            {
                "seq": 2,
                "mission_id": "m",
                "ts": "2026-10-26T00:00:01Z",
                "type": "task.dispatched",
                "actor": "engine",
                "payload": {"task_id": "t-0001", "attempt": 1},
                "idem_key": "td:t-0001:1",
            },
            ensure_ascii=False,
        ),
        _json.dumps(
            {
                "seq": 3,
                "mission_id": "m",
                "ts": "2026-10-26T00:00:02Z",
                "type": "action.started",
                "actor": "gateway",
                "payload": {
                    "tool": "probe",
                    "args": {"target": "h", "port": 80},
                    "fp": "fp1",
                    "task_id": "t-0001",
                    "expect": "open",
                },
                "idem_key": "act:fp1:r1",
            },
            ensure_ascii=False,
        ),
        _json.dumps(
            {
                "seq": 4,
                "mission_id": "m",
                "ts": "2026-10-26T00:00:03Z",
                "type": "decision.made",
                "actor": "worker:t-0001",
                "payload": {
                    "kind": "finish",
                    "task_id": "t-0001",
                    "attempt": 1,
                    "step": 1,
                    "summary": "catch-all，非暴露",
                    "findings_proposed": 1,
                    "findings_skipped": 0,
                },
                "idem_key": "dm:w:t-0001:1:1:finish",
            },
            ensure_ascii=False,
        ),
        _json.dumps(
            {
                "seq": 5,
                "mission_id": "m",
                "ts": "2026-10-26T00:00:04Z",
                "type": "action.finished",
                "actor": "gateway",
                "payload": {
                    "tool": "probe",
                    "fp": "fp1",
                    "exit": 0,
                    "evidence": "ev-x",
                    "task_id": "t-0001",
                },
                "idem_key": "fin:fp1:r1",
            },
            ensure_ascii=False,
        ),
    ]
    (root / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    text = render_log(root)
    assert "- 思考：决定收口" in text
    assert "发现提案：1 条进 JEV 裁决" in text
    assert "exit 0" in text, "action.finished 仍应配对到 started"
    assert render_log(root) == text


def test_http_meta_criterion_requires_headers():
    from pa.core.reflect import criteria_met

    clue = Clue(
        kind="http.status.200",
        key="http://h/",
        value="status=200",
        target="h",
        path="/",
    )
    crit = [Criterion(evidence="http.meta")]
    assert not criteria_met([clue], crit), "200 不等于响应头已采集"
    clue.meta = {"headers": {"content-type": "text/html"}, "content_length": 12}
    assert criteria_met([clue], crit)


def test_http_meta_and_dns_clues(tmp_mission):
    from pa.core.reflect import extract_clues
    from pa.tools import REGISTRY
    from pa.tools.base import ToolContext

    ctx = ToolContext(workspace=tmp_mission, timeout=5)
    # DNS 工具 → dns.result 线索
    r = REGISTRY["dns"].fn({"target": "localhost"}, ctx)
    assert r.exit_code == 0
    out = type(
        "O",
        (),
        {
            "exit_code": r.exit_code,
            "stdout": r.stdout,
            "stderr": r.stderr,
            "evidence_id": "ev-x",
            "meta": r.meta or {},
        },
    )()
    clues = extract_clues("dns", {"target": "localhost"}, out)
    assert clues and clues[0].kind == "dns.result" and clues[0].value["ips"]

    # HTTP meta 线索：结构化 headers/length/hash
    from pa.infra.executor import ProcResult

    http_out = ProcResult(
        200,
        b"hello",
        b"",
        meta={
            "status": 200,
            "headers": {"content-type": "text/plain"},
            "content_length": 5,
            "body_sha256": "abc",
        },
    )
    out2 = type(
        "O",
        (),
        {
            "exit_code": 200,
            "stdout": b"hello",
            "stderr": b"",
            "evidence_id": "ev-y",
            "meta": http_out.meta,
        },
    )()
    clues2 = extract_clues("http", {"url": "http://h/x"}, out2)
    assert clues2 and clues2[0].kind == "http.status.200"
    assert clues2[0].meta["content_length"] == 5
    assert clues2[0].path == "/x"
