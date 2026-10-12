"""漏洞发现/证据/狩猎/覆盖度/报告/验证命令实现（命令注册在 cli/app.py）。

- findings / evidence：漏洞库分诊视图与流量证据包导出
- hunt / coverage：意图驱动狩猎与资产覆盖度
- report / verify / retest / intel：报告交付、对抗式复现、复测、图记忆维护
"""

from __future__ import annotations

import os

import typer


def findings_cmd(
    status: str | None = typer.Option(
        None, "--status", "-s", help="按状态过滤（inferred/confirmed/...）"
    ),
    target: str | None = typer.Option(None, "--target", "-t", help="按目标模糊过滤"),
    show: str | None = typer.Option(None, "--show", help="显示指定 finding 详情"),
    mark: str | None = typer.Option(
        None,
        "--mark",
        help="标记状态（false_positive/duplicate/ignored/resolved/risk_accepted）",
    ),
    classify: str | None = typer.Option(None, "--classify", help="LLM 分类 CWE 并写回该 finding"),
    cvedupe: str | None = typer.Option(
        None, "--cvedupe", help="已知 CVE 去重检查（防把 1day 当 0day 报）"
    ),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
) -> None:
    """漏洞发现库：跨 job 存活的分诊视图（确认纪律：inferred 只能经 verify 升级）。"""
    from secpt.findings.store import FindingStore

    store = FindingStore()
    if cvedupe:
        import asyncio

        from secpt.findings.cve import dedup_known

        f = store.get(cvedupe)
        if f is None:
            typer.echo(f"Error: finding {cvedupe} 不存在", err=True)
            raise typer.Exit(2)
        if os.environ.get("SECPT_FAKE_LLM"):
            typer.echo("Error: CVE 去重需要真实 LLM（FakeLLM 无知识可判）", err=True)
            raise typer.Exit(2)
        cves = asyncio.run(dedup_known(f.title, f.description, f.vuln_class, model=model))
        f.cve_candidates = cves
        store.save(f)
        if cves:
            typer.echo(
                f"⚠️ {f.id} 疑似已知漏洞复现：{'、'.join(cves)}（报告时将标注，别当 0day 报）"
            )
        else:
            typer.echo(f"✅ {f.id} 未发现已知 CVE 对应——可以理直气壮报 0day")
        return
    if classify:
        import asyncio

        from secpt.report import classify as _classify
        from secpt.report import format_cwe

        f = store.get(classify)
        if f is None:
            typer.echo(f"Error: finding {classify} 不存在", err=True)
            raise typer.Exit(2)
        cwes = asyncio.run(_classify(f.title, f.description, model=model))
        if not cwes:
            typer.echo("无可匹配 CWE（可扩充 report/cwe.py 字典）")
            raise typer.Exit(1)
        f.cwe = format_cwe(cwes)
        store.save(f)
        typer.echo(f"{f.id}: {f.cwe}")
        return
    if mark:
        old = store.get(show or "")
        if old is None:
            typer.echo(f"Error: finding {show} 不存在", err=True)
            raise typer.Exit(2)
        try:
            f = store.transition(show or "", mark)
        except ValueError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(2) from e
        typer.echo(f"{f.id}: {old.status} → {f.status}（{f.title}）")
        return
    if show:
        f = store.get(show)
        if f is None:
            typer.echo(f"Error: finding {show} 不存在", err=True)
            raise typer.Exit(2)
        typer.echo(f"id:         {f.id}")
        typer.echo(f"title:      {f.title}")
        typer.echo(
            f"status:     {f.status}  (severity={f.severity}, confidence={f.confidence:.2f})"
        )
        typer.echo(f"target:     {f.target}")
        typer.echo(f"vuln_class: {f.vuln_class}  cwe: {f.cwe or '-'}")
        typer.echo(f"first_seen: {f.first_seen}")
        typer.echo(f"evidence({len(f.evidence)}):")
        for e in f.evidence:
            typer.echo(f"  [{e.role}] {e.kind}: {e.ref[:120]}")
        typer.echo(f"reproduction:\n{f.reproduction}")
        if f.reproduced_by:
            typer.echo("reproductions:")
            for r in f.reproduced_by:
                typer.echo(f"  {r['verdict']}  job={r['job_id']}")
        return
    rows = store.list(status=status, target=target)
    if not rows:
        typer.echo("（无 findings）")
        return
    typer.echo(f"{'id':16s} {'status':13s} {'sev':8s}  title / target")
    for f in rows:
        typer.echo(f"{f.id:16s} {f.status:13s} {f.severity:8s}  {f.title[:40]}  @{f.target[:30]}")


def evidence_cmd(
    action: str = typer.Argument("export", help="export（导出 job 流量证据包）"),
    job_id: str = typer.Argument(..., help="job_id"),
    out: str = typer.Option("evidence-export", "--out", "-o", help="输出目录"),
) -> None:
    """证据包导出：job 的全部流量交换（request/response.http 风格 + manifest.json）。"""
    from secpt.evidence.store import EvidenceStore

    es = EvidenceStore()
    dest = es.export_job(job_id, out)
    if dest is None:
        typer.echo(f"Error: job {job_id} 无流量库（该任务未启用录制或尚无流量）", err=True)
        raise typer.Exit(1)
    typer.echo(f"证据包已导出: {dest}")


def hunt_cmd(
    objective_file: str = typer.Argument(..., help="目标/任务描述文件"),
    profile: str = typer.Option("hunter", "--profile", "-p"),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
    max_rounds: int = typer.Option(20, "--rounds", "-r"),
) -> None:
    """意图驱动狩猎：planner 产意图入 frontier（SQLite 持久化），worker 逐条执行。

    多步利用链的计划不依赖模型上下文记忆——深洞场景（5+ 步）用这个入口。
    """
    import asyncio
    from pathlib import Path as _P

    from secpt.core.orchestrator.frontier import run_frontier

    objective = _P(objective_file).read_text(encoding="utf-8", errors="replace")
    summary = asyncio.run(
        run_frontier(objective, profile=profile, model=model, max_rounds=max_rounds)
    )
    typer.echo("")
    typer.echo(
        f"hunt: {summary['job_id']}  wall={summary['wall_s']}s  "
        f"flags={summary['flags'] or '无'}  open剩余={summary['open_remaining']}"
    )
    for c in summary["chains"]:
        typer.echo(
            f"  [{c['status']:9s}] depth={c['depth']} steps={c['steps']}  {c['intent'][:60]}"
        )
    raise typer.Exit(0 if summary["flags"] else 1)


def coverage_cmd(
    asset_type: str = typer.Option(
        "endpoint", "--type", "-t", help="资产类型（root_domain/subdomain/ip/service/app/endpoint）"
    ),
) -> None:
    """资产覆盖度：哪些已触达、哪些未测（挖洞最怕自以为测完了）+ 未触达清单。"""
    from secpt.intel.store import IntelStore

    st = IntelStore()
    cov = st.coverage()
    typer.echo(f"{'类型':12s} {'总数':>6s} {'已触达':>8s} {'覆盖率':>8s}")
    for t, d in sorted(cov.items()):
        pct = (d["touched"] / d["total"] * 100) if d["total"] else 0.0
        typer.echo(f"{t:12s} {d['total']:>6d} {d['touched']:>8d} {pct:>7.1f}%")
    untested = st.untested_assets(atype=asset_type)
    if untested:
        typer.echo(f"\n未触达 {asset_type}（planner 待办原材料）：")
        for a in untested[:30]:
            typer.echo(f"  #{a['id']} {a['value'][:90]}")


def report_cmd(
    job_id: str | None = typer.Argument(None, help="任务 job_id（缺省=全部发现）"),
    fmt: str = typer.Option("md", "--format", "-f", help="md | md-zip | json"),
    out: str | None = typer.Option(None, "--out", "-o", help="输出路径"),
    stale: bool = typer.Option(False, "--stale", help="列出证据已变更、报告过期的 findings"),
) -> None:
    """生成渗透报告（md/md-zip/json），含 CWE 映射与证据一致性快照。"""
    from secpt.report import ReportDelivery

    rd = ReportDelivery()
    if stale:
        rows = rd.stale_findings()
        if not rows:
            typer.echo("（无过期报告）")
            return
        typer.echo("证据已变更、报告待更新：")
        for f in rows:
            typer.echo(f"  {f.id}  v{f.evidence_version}  {f.title}")
        return
    if fmt not in ("md", "md-zip", "json"):
        typer.echo(f"Error: --format 只支持 md/md-zip/json，收到 {fmt!r}")
        raise typer.Exit(2)
    path = rd.generate(job_id=job_id, fmt=fmt, out=__import__("pathlib").Path(out) if out else None)
    typer.echo(f"报告已生成: {path}")


def verify_cmd(
    finding_id: str = typer.Argument(..., help="要对抗式复现的 finding_id"),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
) -> None:
    """对抗式自检：用与原始复现不同的路径独立再触发（唯一升级 confirmed 的入口）。"""
    import asyncio

    from secpt.findings.verify import verify_finding

    try:
        f = asyncio.run(verify_finding(finding_id, model=model))
    except KeyError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2) from e
    last = f.reproduced_by[-1] if f.reproduced_by else {}
    typer.echo(f"{f.id}: verdict={last.get('verdict')} → status={f.status}")


def retest_cmd(
    finding_id: str = typer.Argument(..., help="要复测的 finding_id"),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
) -> None:
    """复测：最小化重放原始步骤（reproduced / fixed / inconclusive 三档）。"""
    import asyncio

    from secpt.findings.verify import retest_finding

    try:
        f = asyncio.run(retest_finding(finding_id, model=model))
    except KeyError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2) from e
    last = f.reproduced_by[-1] if f.reproduced_by else {}
    typer.echo(f"{f.id}: verdict={last.get('verdict')}（复测不自动降级 confirmed，人工裁决）")


def intel_cmd(
    action: str = typer.Argument("maintain", help="maintain（冷节点压缩）/ search <词>"),
    query: str | None = typer.Argument(None, help="search 的检索词"),
    ttl_days: int = typer.Option(7, "--ttl-days", help="冷化时限（天）"),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
) -> None:
    """图记忆维护：冷节点压缩（图结构判决冷热，签名缓存省 LLM）。"""
    import asyncio

    from secpt.intel.store import COLD_TTL_S, IntelStore

    if action == "search":
        if not query:
            typer.echo("Error: search 需要检索词")
            raise typer.Exit(2)
        hits = IntelStore().recall_graph(query, limit=8)
        for h in hits:
            tag = "🔗" if h.get("associated") else "#"
            typer.echo(f"{tag}{h['id']} [{h['type']}] {h['content'][:140]}")
        return
    if action != "maintain":
        typer.echo(f"Error: 只支持 maintain / search，收到 {action!r}")
        raise typer.Exit(2)
    if os.environ.get("SECPT_FAKE_LLM"):
        typer.echo("Error: 冷压缩需要真实 LLM（FakeLLM 无摘要能力）")
        raise typer.Exit(2)
    from secpt.intel.maintain import maintain

    r = asyncio.run(maintain(ttl_s=COLD_TTL_S / 7 * ttl_days, model=model))
    typer.echo(
        f"冷压缩: 候选 {r['candidates']} → 压缩 {r['compressed']} "
        f"/ 签名缓存命中 {r['cached']} / 跳过 {r['skipped']}"
    )
