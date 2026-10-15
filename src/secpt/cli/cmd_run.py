"""``secpt run`` / ``secpt resume`` 命令实现（命令注册在 cli/app.py）。

- ``run_cmd``：默认 Engine 单链；--mode dag 启用编排；--mode tree 已废弃，自动转 direct
- ``resume_cmd``：从断点恢复任务
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import typer


def _write_tmp_objective(text: str) -> str:
    """把 tree 模式的 target/task 文本落成临时题面文件，返回路径。"""
    tmp = Path("var/tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / f"tree-{uuid.uuid4().hex[:8]}.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def run_cmd(
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
    --mode dag 显式启用多入口编排（frontier 角色意图：recon → exploit×N 并行 → verify）。

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
        # F3：防御读取——summary 契约由 dag._fill_summary_contract 统一补齐，
        # 这里再兜底一次，任何新执行器缺键都不该让 CLI 在跑完后崩报告路径
        typer.echo(f"entries:   {len(summary.get('entries') or [])} 个")
        typer.echo(f"wall_s:    {summary['wall_s']:.1f}")
        typer.echo(
            f"assets:    {summary.get('warroom_assets', 0)}"
            f"  attempted={summary.get('warroom_attempts', 0)}"
        )
        typer.echo(
            f"findings:  {summary.get('findings', 0)}  verified={summary.get('verified', 0)}"
        )
        typer.echo(f"flags:     {summary['flags'] or '未拿到'}")
        for c in summary["chains"]:
            # 统一 ChainResult 契约（结构化轮）：entry>=0 为单链/stages 入口维度，
            # 否则显示意图标题（frontier/benchmark）
            if c.get("entry", -1) >= 0:
                detail = f"entry={c['entry']}"
            else:
                detail = f"intent={str(c.get('intent') or '')[:40]}"
            typer.echo(
                f"  chain:   {c.get('role') or '':<8s} {detail} "
                f"steps={c.get('steps', 0)} wall={c.get('wall_s', 0)}s "
                f"status={c.get('status', '?')}"
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

    # ---- tree 模式（旧 engagement 引擎，已废弃：自动转 direct，行为不中断）----
    typer.echo("--mode tree 已废弃：自动转入 direct（Engine 单链）模式")
    if file and task:
        typer.echo("Error: --file 与 --task 互斥")
        raise typer.Exit(1)
    if not objective_file:
        if file:
            objective_file = file
        elif task:
            objective_file = _write_tmp_objective(task)
        elif target:
            objective_file = _write_tmp_objective(
                f"对 {target} 进行渗透测试，目标是拿到漏洞证明（flag）。"
            )
        else:
            typer.echo("Error: tree 模式需要 target / --task / --file 之一")
            raise typer.Exit(2)
    _run_engine(
        objective_file,
        attach_dir or "",
        profile,
        prior,
        model=model,
        fallback=fallback,
        wall=wall,
    )


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
