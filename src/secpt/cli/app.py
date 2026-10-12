"""typer CLI 入口（薄注册层）。

命令实现按职责拆分到独立模块，此处只做注册：
- ``cli/cmd_run.py``      —— run / resume
- ``cli/cmd_findings.py`` —— findings / evidence / hunt / coverage / report / verify / retest / intel
- ``cli/cmd_jobs.py``     —— jobs / submit / ls / status / tail / cancel / forever
- ``cli/solve.py``、``cli/chat.py``、``cli/watch.py`` —— 一键解题 / 交互会话 / 实时日志

已废弃命令（engage / challenge / tree 模式）保留参数但不中断行为：
提示由新入口取代或自动转 direct 模式。
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from secpt.cli import cmd_ops
from secpt.cli.cmd_findings import (
    coverage_cmd,
    evidence_cmd,
    findings_cmd,
    hunt_cmd,
    intel_cmd,
    report_cmd,
    retest_cmd,
    verify_cmd,
)
from secpt.cli.cmd_jobs import (
    cancel_cmd,
    forever_cmd,
    jobs_cmd,
    ls_cmd,
    status_cmd,
    submit_cmd,
    tail_cmd,
)
from secpt.cli.cmd_run import resume_cmd, run_cmd

app = typer.Typer(help="secpt — security penetration testing agent")


def _doctor_stores() -> None:
    """存储面体检：每个 SQLite 可打开/有 WAL/表计数；跨库一致性警示。"""
    import sqlite3

    from secpt.config.envfile import project_env_file

    var = Path(os.environ.get("SECPT_VAR", "var"))
    dbs = {
        "jobs/events": var / "secpt.sqlite",
        "findings": var / "findings.sqlite",
        "intel(双图)": var / "intel.sqlite",
        "frontier(意图)": var / "frontier.sqlite",
        "steer(操舵)": var / "steer.sqlite",
        "intercept(审批)": var / "intercept.sqlite",
    }
    typer.echo("== 存储体检 ==")
    all_ok = True
    for name, path in dbs.items():
        if not path.is_file():
            typer.echo(f"  {name:16s} ○ 不存在（首次使用前正常）")
            continue
        try:
            with sqlite3.connect(str(path), timeout=5) as c:
                n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                wal = path.with_suffix(path.suffix + "-wal")
                typer.echo(
                    f"  {name:16s} ✓ {path.stat().st_size // 1024}KB {n}表"
                    f"{' (WAL活跃)' if wal.is_file() else ''}"
                )
        except sqlite3.Error as exc:
            all_ok = False
            typer.echo(f"  {name:16s} ✗ {exc}")
    jobs_dir = var / "jobs"
    n_jobs = len(list(jobs_dir.glob("*"))) if jobs_dir.is_dir() else 0
    blob_dir = var / "evidence" / "blobs"
    n_blobs = sum(1 for _ in blob_dir.rglob("*.bin")) if blob_dir.is_dir() else 0
    typer.echo(f"  事件流/工作区   {'✓' if jobs_dir.is_dir() else '○'} {n_jobs} 个 job")
    typer.echo(f"  证据 blob       {'✓' if blob_dir.is_dir() else '○'} {n_blobs} 个")
    typer.echo(
        "  一致性边界：跨库写已按幂等设计（sha1/内容寻址/原子认领），"
        "无跨库事务——doctor 只做可开/可写体检，业务一致性靠幂等兜底。"
    )
    if not project_env_file().is_file() and not os.environ.get("DEEPSEEK_API_KEY"):
        typer.echo("  ⚠️ 无 .env 且无 DEEPSEEK_API_KEY（export 或 cp .env.example .env）")
    if not all_ok:
        raise typer.Exit(1)


@app.command()
def doctor(
    stores: bool = typer.Option(False, "--stores", help="各 SQLite 存储健康/一致性检查"),
) -> None:
    """检查环境与配置（--stores 追加存储面体检）。"""
    if stores:
        _doctor_stores()
    import secpt
    from secpt.runtime.llm.client import resolve_model

    typer.echo(f"app.version       = {secpt.__version__}")

    # 模型路由检查（多 provider / 故障转移池）
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY"))
    typer.echo(f"llm.api_key_env   = DEEPSEEK_API_KEY  {'✓ set' if has_key else '✗ NOT SET'}")
    if os.environ.get("SECPT_MODEL_POOL"):
        typer.echo(f"llm.pool          = {os.environ['SECPT_MODEL_POOL']}")
    from secpt.cli.engine_runner import resolve_models

    actual_primary, actual_fallback = resolve_models(None, None)
    try:
        base, _key, mid = resolve_model(actual_primary)
        typer.echo(f"llm.route         = ✓ {actual_primary} -> {base} ({mid})")
        typer.echo(f"llm.fallback      = {actual_fallback}")
    except RuntimeError as exc:
        typer.echo(f"llm.route         = ✗ {exc}")
        raise typer.Exit(1) from exc

    # 沙箱与插件
    from secpt.runtime.sandbox import docker_available
    from secpt.tools.plugin_loader import discover_plugins

    if os.environ.get("SECPT_SANDBOX") == "0":
        typer.echo("sandbox           = 关闭（SECPT_SANDBOX=0）")
    else:
        ok = docker_available()
        typer.echo(f"sandbox           = {'✓ docker 可用' if ok else '✗ 不可用（回退宿主执行）'}")
    yp, pt = discover_plugins()
    typer.echo(
        f"plugins           = YAML:{sorted(yp) or '无'} Python:{sorted(pt) or '无'}"
        "（tools/plugins/ 声明即装配）"
    )

    if os.environ.get("SECPT_FAKE_LLM"):
        typer.echo("llm.adapter       = FakeLLM (dry-run)")
    elif not has_key:
        typer.echo("llm.adapter       = ✗ missing API key")
        raise typer.Exit(1)

    typer.echo("doctor: OK")


app.command("run")(run_cmd)
app.command("resume")(resume_cmd)


@app.command("vpn")
def vpn_entry(
    action: str = typer.Argument("status", help="status | up | down"),
) -> None:
    """VPN 自举：查看/拉起/关闭 VPN（benchmark 内网任务前置；SECPT_VPN_TYPE/CONFIG 配置）。"""
    from secpt.runtime import vpn

    if action == "status":
        typer.echo(
            f"VPN: {'✓ 已连通' if vpn.status() else '✗ 未连通'}"
            f"（type={vpn.vpn_type()} configured={vpn.vpn_configured()}）"
        )
        raise typer.Exit(0 if vpn.status() else 1)
    if action == "up":
        ok, msg = vpn.up()
        typer.echo(msg)
        raise typer.Exit(0 if ok else 1)
    if action == "down":
        ok, msg = vpn.down()
        typer.echo(msg)
        raise typer.Exit(0 if ok else 1)
    typer.echo(f"Error: 只支持 status/up/down，收到 {action!r}")
    raise typer.Exit(2)


@app.command("steer")
def steer_entry(
    job_id: str = typer.Argument(..., help="目标 job_id"),
    msg: str = typer.Argument(..., help="操舵提示（不打断任务，下一步即生效）"),
) -> None:
    """人在环路-中途纠偏：给正在运行的任务注入提示（ARTEX SteerWork 同款）。"""
    from secpt.cli.cmd_ops import steer_cmd

    steer_cmd(job_id, msg)


@app.command("pending")
def pending_entry() -> None:
    """查看等待人工审批的拦截请求（配合 secpt approve/deny）。"""
    from secpt.cli.cmd_ops import pending_cmd

    pending_cmd()


@app.command("approve")
def approve_entry(pending_id: str = typer.Argument(...)) -> None:
    """放行被 ask 规则拦截的命令。"""
    from secpt.cli.cmd_ops import decide_cmd

    decide_cmd(pending_id, True)


@app.command("deny")
def deny_entry(pending_id: str = typer.Argument(...)) -> None:
    """拒绝被 ask 规则拦截的命令。"""
    from secpt.cli.cmd_ops import decide_cmd

    decide_cmd(pending_id, False)


@app.command("metrics")
def metrics_entry(
    fmt: str = typer.Option("table", "--format", "-f", help="table|json"),
    snapshot: bool = typer.Option(False, "--snapshot", help="记录本次指标到历史"),
    trend: int = typer.Option(0, "--trend", "-n", help="与最近 N 次快照对比"),
) -> None:
    """运营指标：成功率/误报率/成本/干预次数；--snapshot 记历史，--trend N 看趋势。"""
    from secpt.cli.cmd_ops import metrics_cmd

    metrics_cmd(fmt, snapshot=snapshot, trend=trend)


app.add_typer(cmd_ops.intent_app, name="intent")
app.add_typer(cmd_ops.intercept_app, name="intercept")


@app.command("logs")
def logs_entry(
    action: str = typer.Argument("export", help="export（导出可读日志）"),
    job_id: str = typer.Argument(..., help="job_id"),
    out: str = typer.Option(None, "--out", "-o", help="输出路径（缺省 var/jobs/<id>/run.log）"),
    fmt: str = typer.Option("txt", "--format", "-f", help="txt | md | json"),
) -> None:
    """导出完整可读日志（txt=终端同款渲染 / md=带元信息报告向 / json=原始事件）。"""
    from secpt.cli.watch import export_log

    if action != "export":
        typer.echo(f"Error: 只支持 export，收到 {action!r}")
        raise typer.Exit(2)
    if fmt not in ("txt", "md", "json"):
        typer.echo("Error: --format 只支持 txt/md/json")
        raise typer.Exit(2)
    out = out or f"var/jobs/{job_id}/run.log"
    try:
        p = export_log(job_id, out, fmt=fmt)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2) from e
    typer.echo(f"日志已导出: {p}")


@app.command("graph")
def graph_entry(
    gtype: str = typer.Option("assets", "--type", "-t", help="assets|exploration"),
    fmt: str = typer.Option("mermaid", "--format", "-f", help="mermaid|dot"),
) -> None:
    """双图可视化导出（贴进 Markdown/Typora 即看图；exploration=血缘链）。"""
    from secpt.cli.cmd_ops import graph_export_cmd

    graph_export_cmd(gtype, fmt)


@app.command("solve")
def solve_entry(
    target: str = typer.Argument(..., help="挑战目录或题面文件（test.md/题目.md）"),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="任务 profile（默认自动路由：规则关键词→LLM 兜底）"
    ),
    prior: str | None = typer.Option(None, "--prior", help="先验覆盖（默认按关键词自动匹配）"),
    model: str | None = typer.Option(
        None, "--model", "-m", help="主模型（默认读 SECPT_MODEL_PRIMARY，缺省 deepseek-chat）"
    ),
    fallback: str | None = typer.Option(
        None, "--fallback", help="回退模型（默认读 SECPT_MODEL_FALLBACK）"
    ),
    wall: int = typer.Option(-1, "--wall", help="墙钟上限秒：-1=profile 默认，0=不限"),
    steps: int = typer.Option(0, "--steps", help="步数上限：0=profile 默认"),
    mode: str = typer.Option(
        "auto", "--mode", help="执行模式：auto（默认）/ single / frontier / stages"
    ),
    no_judge: bool = typer.Option(False, "--no-judge", help="跑完不自动评判"),
    no_learn: bool = typer.Option(False, "--no-learn", help="跑完不自动沉淀经验"),
    no_watch: bool = typer.Option(False, "--no-watch", help="不打印实时交互日志（脚本/CI 用）"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """一键解题（唯一用户入口）：自动选择打法，启动即自带实时交互日志。"""
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
        watch_enable=not no_watch,
    )
    raise typer.Exit(code)


for _name, _fn in [
    ("findings", findings_cmd),
    ("evidence", evidence_cmd),
    ("hunt", hunt_cmd),
    ("coverage", coverage_cmd),
    ("report", report_cmd),
    ("verify", verify_cmd),
    ("retest", retest_cmd),
    ("intel", intel_cmd),
]:
    app.command(_name)(_fn)


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


for _name, _fn in [
    ("jobs", jobs_cmd),
    ("submit", submit_cmd),
    ("ls", ls_cmd),
    ("status", status_cmd),
    ("tail", tail_cmd),
    ("cancel", cancel_cmd),
    ("forever", forever_cmd),
]:
    app.command(_name)(_fn)


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


if __name__ == "__main__":
    app()
