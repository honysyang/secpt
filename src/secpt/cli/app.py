"""typer CLI 入口。

暴露：
- ``secpt --help``
- ``secpt doctor``  —— 检查环境/配置/白名单/GraphStore
- ``secpt run --target <host> [--fake]``  —— ReAct 单任务
- ``secpt engage --objective ... --target ... [--fake]``  —— 完整 engagement（分形树）
- ``secpt challenge <id> [--fake]``  —— 加载题目并自动渗透
- ``secpt challenge --list``  —— 列出所有题目
- ``secpt challenge --scaffold <id>``  —— 创建题目骨架
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

import typer

app = typer.Typer(help="secpt — security penetration testing agent")


@app.command()
def doctor() -> None:
    """检查环境与配置。"""
    from secpt.config.settings import get_settings
    from secpt.core.graph.factory import get_graph_store

    settings = get_settings()

    typer.echo(f"app.name          = {settings.app.name}")
    typer.echo(f"app.version       = {settings.app.version}")
    typer.echo(f"llm.provider      = {settings.llm.provider}")
    typer.echo(f"llm.planning      = {settings.llm.planning_model}")
    typer.echo(f"llm.eval          = {settings.llm.eval_model}")
    typer.echo(f"llm.fake          = {settings.llm.fake}")
    typer.echo(f"graph.backend     = {settings.graph.backend}")
    typer.echo(f"tools.command_whitelist = {settings.tools.command_whitelist}")
    typer.echo(f"tools.tool_names   = {settings.tools.tool_names}")
    typer.echo(f"react.max_steps   = {settings.react.max_steps}")

    # 打印实际注册的工具清单
    from secpt.core.tools.registry import build_registry

    reg = build_registry(
        whitelist=settings.tools.command_whitelist,
        tool_names=settings.tools.tool_names,
    )
    typer.echo("")
    typer.echo("已注册工具:")
    for t in reg.all():
        typer.echo(f"  {t.name:20s} {t.description[:60]}")

    # 检查 Web/SSO 工具
    reg_names = set(reg.names())
    missing = {"saml", "crawler"} - reg_names
    if missing:
        typer.echo(
            f"⚠️  缺少 Web/SSO 工具: {', '.join(missing)}（Web/SSO 类题目需要 saml + crawler）"
        )

    import os

    key_env = settings.llm.api_key_env
    has_key = bool(os.environ.get(key_env))
    typer.echo(f"llm.api_key_env   = {key_env}  {'✓ set' if has_key else '✗ NOT SET'}")

    for name in settings.tools.whitelist:
        path = shutil.which(name)
        status = f"✓ {path}" if path else "✗ NOT FOUND in PATH"
        typer.echo(f"  tool {name:10s} {status}")

    try:
        store = get_graph_store(settings.graph)
        asyncio.run(store.close())
        typer.echo(f"graph.store       = ✓ ({settings.graph.backend})")
    except Exception as exc:
        typer.echo(f"graph.store       = ✗ {exc}")
        raise typer.Exit(1) from exc

    if settings.llm.fake:
        typer.echo("llm.adapter       = FakeLLM (dry-run)")
    else:
        if not has_key:
            typer.echo("llm.adapter       = ✗ missing API key")
            raise typer.Exit(1)
        typer.echo("llm.adapter       = LiteLLM (DeepSeek)")

    typer.echo("doctor: OK")


@app.command("run")
def run_react_cmd(
    target: str | None = typer.Argument(None, help="目标 host / url / domain"),
    task: str | None = typer.Option(None, "--task", "-t", help="自定义任务描述"),
    max_steps: int | None = typer.Option(None, "--max-steps", "-n", help="ReAct 最大步数"),
    fake: bool = typer.Option(False, "--fake", "-f", help="用 FakeLLM dry-run（无需 API key）"),
    show_trajectory: bool = typer.Option(False, "--trajectory", help="打印 trajectory"),
    file: str | None = typer.Option(None, "--file", help="题面文件路径（与 --task 互斥）"),
    objective_file: str | None = typer.Option(
        None, "--objective-file", help="题面文件路径（direct 模式）"
    ),
    attach_dir: str | None = typer.Option(None, "--attach-dir", help="附件目录（direct 模式）"),
    profile: str = typer.Option(
        "ctf", "--profile", "-p", help="先验 profile（对应 knowledge/priors/<name>.md）"
    ),
    prior: str | None = typer.Option(
        None, "--prior", help="覆盖先验文件（knowledge/priors/<name>.md），如 saml"
    ),
    mode: str = typer.Option(
        "direct",
        "--mode",
        help="执行模式：direct=Engine 单链（默认），tree=原 engagement，dag=显式启用编排",
    ),
    model: str = typer.Option(
        "deepseek-chat",
        "--model",
        "-m",
        help="主模型（支持 provider/model 形式，如 openai/gpt-4o）",
    ),
    fallback: str = typer.Option("deepseek-reasoner", "--fallback", help="失败回退模型"),
    wall: int = typer.Option(
        -1,
        "--wall",
        help="墙钟上限秒数：-1=用 profile 默认（缺省），0=不限（7×24 长任务），>0=该秒数",
    ),
    orchestrator: bool = typer.Option(
        False,
        "--orchestrator/--no-orchestrator",
        help="多入口任务是否启用 DAG 编排（默认关闭，对照组结论；--orchestrator 或 --mode dag 显式开启）",
    ),
) -> None:
    """对 ``target`` 执行一次渗透任务。

    默认走 Engine 单链（function-calling），失败自动换路线重试一次；
    --mode dag 显式启用 Specialist DAG 编排（recon → exploit×N 并行 → verify）。

    S5 多入口对照结论：DAG 层保留但默认关闭；只有显式 --mode dag（或 --orchestrator）
    才启用。entries <= 1 时编排层天然休眠，直连单链。
    """
    if mode not in ("direct", "tree", "dag"):
        typer.echo(f"Error: --mode 只支持 direct / tree / dag，收到 {mode!r}")
        raise typer.Exit(2)

    # ---- S5：--task 指向 TaskSpec yaml → orchestrator（DAG / 单链分流）----
    from pathlib import Path as _P

    if task and _P(task).is_file() and _P(task).suffix.lower() in (".yaml", ".yml"):
        import yaml as _yaml

        from secpt.core.job.models import TaskSpec as _TaskSpec
        from secpt.core.orchestrator.dag import run_task

        spec = _TaskSpec.model_validate(_yaml.safe_load(_P(task).read_text(encoding="utf-8")))
        use_dag = orchestrator or (mode == "dag")
        summary = asyncio.run(run_task(spec, use_orchestrator=use_dag))
        typer.echo("")
        typer.echo(f"job_id:    {summary['job_id']}")
        typer.echo(f"mode:      {summary['mode']}")
        typer.echo(f"entries:   {len(summary['entries'])} 个")
        typer.echo(f"wall_s:    {summary['wall_s']:.1f}")
        typer.echo(
            f"assets:    {summary['warroom_assets']}  attempted={summary['warroom_attempts']}"
        )
        typer.echo(f"findings:  {summary['findings']}  verified={summary['verified']}")
        typer.echo(f"flags:     {summary['flags'] or '未拿到'}")
        for c in summary["chains"]:
            typer.echo(
                f"  chain:   {c['role']:<8s} entry={c['entry']} steps={c['steps']} "
                f"wall={c['wall_s']}s status={c['status']}"
            )
        if not summary["flags"]:
            raise typer.Exit(1)
        return

    if mode == "direct":
        if not objective_file:
            typer.echo("Error: direct 模式需要 --objective-file <题面文件>")
            raise typer.Exit(2)
        if file or task:
            typer.echo("Error: --file / --task 与 direct 模式互斥（仅 tree 模式可用）")
            raise typer.Exit(1)
        _run_engine(
            objective_file,
            attach_dir or "",
            profile,
            prior,
            model=model,
            fallback=fallback,
            wall=wall,
        )
        return

    # ---- tree 模式（原 engine，保留但不默认）----
    if target is None:
        typer.echo("Error: tree 模式需要 target 参数")
        raise typer.Exit(2)
    if file and task:
        typer.echo("Error: --file 与 --task 互斥")
        raise typer.Exit(1)
    if file:
        task = _P(file).read_text(encoding="utf-8", errors="replace")

    from secpt.runtime.runner import run_sync

    result = run_sync(target, task=task, max_steps=max_steps, fake=fake)

    typer.echo(f"\ntarget:    {target}")
    typer.echo(f"backend:   {result.graph_backend}")
    typer.echo(f"steps:     {result.steps}")
    typer.echo(f"exhausted: {result.exhausted}")
    typer.echo(f"answer:\n{result.answer}\n")

    if show_trajectory:
        for ev in result.trajectory:
            typer.echo(json.dumps(ev, ensure_ascii=False, default=str))


def _run_engine(
    objective_file: str,
    attach_dir: str,
    profile: str,
    prior: str | None = None,
    model: str = "deepseek-chat",
    fallback: str = "deepseek-reasoner",
    wall: int = -1,
) -> None:
    """直接调用 Engine（不再走 subprocess），全程落盘到 var/jobs/。"""
    from pathlib import Path as _P

    from secpt.cli.engine_runner import run_engine

    # 读题面
    challenge_text = _P(objective_file).read_text(errors="replace")
    if attach_dir:
        ad = _P(attach_dir)
        if ad.is_dir():
            for f in sorted(ad.rglob("*")):
                if f.suffix in (".md", ".txt") and f.is_file():
                    challenge_text += f"\n\n--- {f} ---\n" + f.read_text(errors="replace")

    result = run_engine(
        objective=challenge_text,
        profile=profile,
        prior=prior,
        model_primary=model,
        model_fallback=fallback,
        max_wall_s=None if wall < 0 else wall,
    )

    typer.echo("")
    typer.echo(f"job_id: {result.job_id}")
    typer.echo(f"steps: {result.steps}")
    typer.echo(f"wall_s: {result.wall_s:.1f}")
    typer.echo(f"status: {result.status}")
    typer.echo(f"flag:   {result.flag or '未拿到'}")
    if not result.flag:
        raise typer.Exit(1)


@app.command("resume")
def resume_cmd(
    job_id: str = typer.Argument(..., help="要恢复的 job_id（var/jobs/<job_id>/）"),
) -> None:
    """从断点继续一个任务（S1 持久化 resume）。"""
    from secpt.cli.engine_runner import resume_engine

    try:
        result = resume_engine(job_id)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2) from e

    typer.echo("")
    typer.echo(f"job_id: {result.job_id}")
    typer.echo(f"steps:  {result.steps}")
    typer.echo(f"wall_s: {result.wall_s:.1f}")
    typer.echo(f"status: {result.status}")
    typer.echo(f"flag:   {result.flag or '未拿到'}")
    if not result.flag:
        raise typer.Exit(1)


@app.command("solve")
def solve_entry(
    target: str = typer.Argument(..., help="挑战目录或题面文件（test.md/题目.md）"),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="任务 profile（默认自动路由：规则关键词→LLM 兜底）"
    ),
    prior: str | None = typer.Option(None, "--prior", help="先验覆盖（默认按关键词自动匹配）"),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
    fallback: str = typer.Option("deepseek-reasoner", "--fallback"),
    wall: int = typer.Option(-1, "--wall", help="墙钟上限秒：-1=profile 默认，0=不限"),
    steps: int = typer.Option(0, "--steps", help="步数上限：0=profile 默认"),
    mode: str = typer.Option(
        "auto", "--mode", help="执行模式：auto（默认）/ single / frontier / stages"
    ),
    no_judge: bool = typer.Option(False, "--no-judge", help="跑完不自动评判"),
    no_learn: bool = typer.Option(False, "--no-learn", help="跑完不自动沉淀经验"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """一键解题（唯一用户入口）：任务类型与打法自动选择，全自动执行-评判-学习。"""
    from secpt.cli.solve import solve

    code = solve(
        target,
        profile=profile,
        prior=prior,
        model=model,
        fallback=fallback,
        wall=wall,
        steps=steps,
        mode=mode,
        judge_enable=not no_judge,
        learn_enable=not no_learn,
        verbose=verbose,
    )
    raise typer.Exit(code)


@app.command("findings")
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


@app.command("evidence")
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


@app.command("hunt")
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


@app.command("coverage")
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


@app.command("report")
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


@app.command("verify")
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


@app.command("retest")
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


@app.command("intel")
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


@app.command("watch")
def watch_entry(
    job_id: str = typer.Argument(..., help="要观察的 job_id"),
    tail: int = typer.Option(0, "--tail", "-n", help="先回看最近 N 条事件再跟随"),
    raw: bool = typer.Option(False, "--raw", help="原始 JSONL（调试用）"),
) -> None:
    """实时交互日志：AI 逐步思考 / 工具调用 / 工具响应 / 发现 / 压缩。"""
    from secpt.cli.watch import watch

    watch(job_id, tail=tail, raw=raw)


@app.command("learn")
def learn_entry(
    job_id: str | None = typer.Argument(None, help="job_id（缺省=最近一个）"),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
) -> None:
    """手动触发经验蒸馏（learning 闭环补漏）。"""
    import asyncio

    from secpt.core.job.store import JobStore
    from secpt.learning.distill import distill_job

    if job_id is None:
        rows = JobStore().list_jobs(limit=1)
        if not rows:
            typer.echo("Error: 无历史 job")
            raise typer.Exit(2)
        job_id = rows[0].job_id
    exp = asyncio.run(distill_job(job_id, model=model))
    if exp:
        typer.echo(f"已沉淀经验：{exp.title}（technique={exp.technique}, outcome={exp.outcome}）")
    else:
        typer.echo("无经验可沉淀（无 transcript / LLM 输出不可解析 / 指纹重复）")


@app.command("judge")
def judge_cmd(
    job_id: str = typer.Argument(..., help="要评判的 job_id（var/jobs/<job_id>/）"),
    ground_truth_file: str | None = typer.Option(
        None, "--ground-truth", help="出题方标准答案/完成态文件（CTF 场景即 flag 原文）"
    ),
    rubric_file: str | None = typer.Option(
        None, "--rubric", help="rubric 文件（YAML/JSON）；缺省由题面自动生成"
    ),
    model: str = typer.Option("deepseek-chat", "--model", "-m", help="终裁 judge 用的模型"),
) -> None:
    """AI 评判一次运行（不依赖 flag 正则）：反作弊 → Jev 门控 → LLM 终裁。

    结果落盘 var/jobs/<job_id>/verdict.json 并追加 verdict 事件。
    SECPT_FAKE_LLM=1 时走 FakeLLM（heuristic 兜底路径，无需 key）。
    """
    import asyncio

    from secpt.evaluation.verdict import evaluate_job, render

    try:
        verdict = asyncio.run(
            evaluate_job(
                job_id,
                rubric_file=rubric_file,
                ground_truth_file=ground_truth_file,
                model=model,
            )
        )
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2) from e

    typer.echo("")
    typer.echo(render(verdict))
    raise typer.Exit(0 if verdict.complete else 1)


@app.command("jobs")
def jobs_cmd(
    status: str | None = typer.Option(None, "--status", "-s", help="按状态过滤"),
) -> None:
    """列出 var/jobs/ 下的作业。"""
    from secpt.core.job.store import JobStore

    store = JobStore()
    rows = store.list_jobs(status=status)
    if not rows:
        typer.echo("（无作业）")
        return
    typer.echo(f"{'job_id':12s} {'status':16s} {'steps':>5s} {'round':>5s}  created")
    for r in rows:
        typer.echo(
            f"{r.job_id:12s} {r.status:16s} {r.steps:>5d} {r.round:>5d}  {r.created_at[:19]}"
        )


# ---------------------------------------------------------------------------
# S2 作业层 CLI
# ---------------------------------------------------------------------------


@app.command("submit")
def submit_cmd(
    task: str = typer.Option(..., "--task", "-t", help="任务 yaml 文件路径"),
    max_parallel: int = typer.Option(4, "--max-parallel", "-j", help="并发上限"),
) -> None:
    """提交任务到作业队列（TaskSpec yaml）。"""
    from pathlib import Path

    import yaml

    from secpt.core.job.manager import JobManager
    from secpt.core.job.models import TaskSpec

    data = yaml.safe_load(Path(task).read_text(encoding="utf-8"))
    spec = TaskSpec.model_validate(data)
    mgr = JobManager(max_parallel_jobs=max_parallel)
    mgr.submit(spec)
    typer.echo(f"已提交: {spec.job_id} (task_type={spec.task_type})")
    mgr.reap()


@app.command("ls")
def ls_cmd(
    status: str | None = typer.Option(None, "--status", "-s", help="按状态过滤"),
    max_parallel: int = typer.Option(4, "--max-parallel", "-j", help="并发上限"),
) -> None:
    """列出作业（状态/步数/耗时）。"""
    from secpt.core.job.manager import JobManager

    mgr = JobManager(max_parallel_jobs=max_parallel)
    mgr.reap()
    rows = mgr.store.list_jobs(status=status)
    if not rows:
        typer.echo("（无作业）")
        return
    typer.echo(f"{'job_id':12s} {'status':16s} {'steps':>5s} {'round':>5s}  created")
    for r in rows:
        typer.echo(
            f"{r.job_id:12s} {r.status:16s} {r.steps:>5d} {r.round:>5d}  {r.created_at[:19]}"
        )


@app.command("status")
def status_cmd(
    job_id: str = typer.Argument(..., help="job_id"),
) -> None:
    """查看单个 job 详情。"""
    from secpt.core.job.manager import JobManager

    mgr = JobManager()
    mgr.reap()
    rec = mgr.get(job_id)
    if rec is None:
        typer.echo(f"Error: job {job_id} 不存在", err=True)
        raise typer.Exit(2)
    typer.echo(f"job_id:   {rec.job_id}")
    typer.echo(f"status:   {rec.status}")
    typer.echo(f"steps:    {rec.steps}")
    typer.echo(f"round:    {rec.round}")
    typer.echo(f"cost_cny: {rec.cost_cny}")
    typer.echo(f"created:  {rec.created_at}")
    typer.echo(f"updated:  {rec.updated_at}")
    if rec.last_error:
        typer.echo(f"error:    {rec.last_error}")
    typer.echo(f"objective: {rec.spec.objective[:120]}")


@app.command("tail")
def tail_cmd(
    job_id: str = typer.Argument(..., help="job_id"),
    n: int = typer.Option(50, "-n", help="最近 N 条事件"),
) -> None:
    """从 events.jsonl 回放最近 N 条事件。"""
    import json as _json

    from secpt.core.job.resume import jobs_dir

    ep = jobs_dir() / job_id / "events.jsonl"
    if not ep.is_file():
        typer.echo(f"Error: job {job_id} 无 events.jsonl", err=True)
        raise typer.Exit(2)
    lines = [ln for ln in ep.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for ln in lines[-n:]:
        e = _json.loads(ln)
        payload = e.get("payload", {})
        brief = str(payload)[:120]
        typer.echo(f"  seq={e['seq']} [{e['type']}] {brief}")


@app.command("cancel")
def cancel_cmd(
    job_id: str = typer.Argument(..., help="job_id"),
    max_parallel: int = typer.Option(4, "--max-parallel", "-j", help="并发上限"),
) -> None:
    """取消一个 job（SIGTERM worker / 出队）。"""
    from secpt.core.job.manager import JobManager

    mgr = JobManager(max_parallel_jobs=max_parallel)
    ok = mgr.cancel(job_id)
    if not ok:
        typer.echo(f"Error: job {job_id} 不存在或不可取消", err=True)
        raise typer.Exit(1)
    typer.echo(f"已取消: {job_id}")


@app.command("forever")
def forever_cmd(
    max_parallel: int = typer.Option(4, "--max-parallel", "-j", help="并发上限"),
) -> None:
    """常驻 manager：轮询回收 + stuck 检测。Ctrl-C 退出。"""
    from secpt.core.job.manager import JobManager

    mgr = JobManager(max_parallel_jobs=max_parallel)
    typer.echo(f"[manager] 常驻运行中，并发上限 {mgr.max_parallel_jobs}（Ctrl-C 退出）")
    try:
        mgr.run_forever()
    except KeyboardInterrupt:
        typer.echo("\n[manager] 退出")


# ---------------------------------------------------------------------------
# S5：旧分形树引擎命令退役（正式删除 core/evaluator 与 core/node 后废弃）
# 保留空壳命令：旧脚本调用不再报 UsageError，统一提示由 orchestrator 取代。
# ---------------------------------------------------------------------------


@app.command("engage")
def engage_cmd(
    target: str | None = typer.Option(
        None, "--target", help="目标 host / url / domain（旧参数，已忽略）"
    ),
    file: str | None = typer.Option(None, "--file", help="题面文件路径（旧参数，已忽略）"),
    objective: str | None = typer.Option(None, "--objective", help="目标描述（旧参数，已忽略）"),
    fake: bool = typer.Option(False, "--fake", help="FakeLLM 干跑（旧参数，已忽略）"),
) -> None:
    """旧分形树 engagement（已废弃）。S5 起多入口由 Specialist DAG 编排取代。"""
    typer.echo("engage 已废弃：S5 起用 secpt run --task <multi-entry.yaml> 走 Specialist DAG 编排")
    raise typer.Exit(1)


@app.command("challenge")
def challenge_cmd() -> None:
    """旧挑战自动渗透（已废弃）。S5 起由 orchestrator 取代。"""
    typer.echo("challenge 已废弃：S5 起用 secpt run --task <TaskSpec.yaml> 走 orchestrator")
    raise typer.Exit(1)


@app.command("chat")
def chat(
    backend: str = typer.Option("memory", "--backend", help="GraphStore 后端 (memory/sqlite)"),
    budget: float = typer.Option(50.0, "--budget", "-b", help="总预算"),
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="最大并发"),
    session_id: str | None = typer.Option(None, "--session", "-s", help="会话 ID（默认自动生成）"),
    fake: bool = typer.Option(False, "--fake", "-f", help="FakeLLM 干跑"),
) -> None:
    """进入交互式会话（自然语言即任务）。"""
    from secpt.cli.chat import chat_cmd

    chat_cmd(
        backend=backend,
        budget=budget,
        concurrency=concurrency,
        session_id=session_id,
        fake=fake,
    )


if __name__ == "__main__":
    app()
