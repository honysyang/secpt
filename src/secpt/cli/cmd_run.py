"""``secpt run`` / ``secpt resume`` 命令实现（命令注册在 cli/app.py）。

入口策略（solve 为唯一用户入口）：
- ``--task <TaskSpec.yaml>``：机器/编排入口，直转 DAG 分流（保留，脚本/平台用）
- ``--mode direct`` / ``tree``（旧）：**已废弃**——自动转发 ``secpt solve`` 单链模式，
  获得完整能力链（理解→拆解→治理→止损→评判→学习）；不再维护裸 Engine 旁路
- ``secpt resume``：断点恢复不变
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import typer


def _write_tmp_objective(text: str) -> str:
    """把 task/target 文本落成临时题面文件，返回路径。"""
    tmp = Path("var/tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / f"tree-{uuid.uuid4().hex[:8]}.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def run_cmd(
    target: str | None = typer.Argument(None, help="目标 host / url / domain（旧参数，已忽略）"),
    task: str | None = typer.Option(None, "--task", "-t", help="自定义任务描述或 TaskSpec yaml"),
    max_steps: int | None = typer.Option(
        None, "--max-steps", "-n", help="步数上限（转发 solve --steps）"
    ),
    fake: bool = typer.Option(
        False, "--fake", "-f", help="FakeLLM 干跑： understanding/评判/学习一并关闭"
    ),
    show_trajectory: bool = typer.Option(
        False, "--trajectory", help="已废弃：跑完用 `secpt logs export <job_id> --format json`"
    ),
    file: str | None = typer.Option(
        None, "--file", help="题面文件路径（旧参数，同 --objective-file）"
    ),
    objective_file: str | None = typer.Option(
        None, "--objective-file", help="题面文件路径（direct 旧参数，同 --file）"
    ),
    attach_dir: str | None = typer.Option(
        None, "--attach-dir", help="附件目录（旧参数；solve 会自动发现 challenge 目录附件）"
    ),
    profile: str = typer.Option(
        "ctf", "--profile", "-p", help="先验 profile（对应 knowledge/priors/<name>.md）"
    ),
    prior: str | None = typer.Option(
        None, "--prior", help="覆盖先验文件（knowledge/priors/<name>.md），如 saml"
    ),
    mode: str = typer.Option(
        "direct",
        "--mode",
        help="执行模式：direct/tree 已废弃（自动转发 solve）；dag=显式启用编排（需 --task yaml）",
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
        help="多入口任务是否启用 DAG 编排（默认关闭；--orchestrator 或 --mode dag 显式开启）",
    ),
) -> None:
    """对 ``target`` 执行一次渗透任务（solve 唯一入口的兼容 shim）。

    旧 direct/tree 参数自动转发 ``secpt solve``（单链模式 + 完整评判/学习链）；
    机器编排入口保留：``--task <TaskSpec.yaml>`` 直转 DAG 分流。
    """
    if mode not in ("direct", "tree", "dag"):
        typer.echo(f"Error: --mode 只支持 direct / tree / dag，收到 {mode!r}")
        raise typer.Exit(2)

    # ---- 机器编排入口：--task 指向 TaskSpec yaml → orchestrator（DAG / 单链分流）----
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

    # ---- direct / tree / dag（无 yaml）：统一转发 secpt solve（唯一用户入口）----
    if mode == "dag":
        typer.echo("Error: --mode dag 需要 --task <TaskSpec.yaml>（编排为机器入口）")
        raise typer.Exit(2)
    if attach_dir:
        typer.echo(
            "注意: --attach-dir 已忽略（solve 自动发现 challenge 目录下的 attachments/附件/）"
        )

    typer.echo(
        f"--mode {mode} 已废弃：自动转发 secpt solve（单链模式，含理解/拆解/止损/评判/学习全链）。"
        " 新用法: secpt solve <题面文件>"
    )
    ofile = objective_file or file
    if ofile and task:
        typer.echo("注意: --task 与题面文件同时给出，使用题面文件（--task 文本已忽略）")
    if not ofile:
        if task:
            ofile = _write_tmp_objective(task)
        elif target:
            ofile = _write_tmp_objective(f"对 {target} 进行渗透测试，目标是拿到漏洞证明（flag）。")
        else:
            typer.echo("Error: 需要 --objective-file <题面文件>（或 --task / target）")
            raise typer.Exit(2)

    if fake:
        # 干跑口径：理解/拆解/judge/learning 都走真 LLM 会破环 dry-run 语义——一并关闭
        import os as _os

        _os.environ["SECPT_FAKE_LLM"] = "1"
        _os.environ["SECPT_UNDERSTANDING"] = "0"
        _os.environ["SECPT_GOALS"] = "0"
    if show_trajectory:
        typer.echo(
            "注意: --trajectory 已废弃——跑完用 secpt logs export <job_id> --format json 看轨迹"
        )

    from secpt.cli.solve import solve

    code = solve(
        ofile,
        profile=profile,
        prior=prior,
        model=model,
        fallback=fallback,
        wall=wall,
        steps=max_steps or 0,
        mode="single",
        judge_enable=not fake,
        learn_enable=not fake,
        watch_enable=False,
    )
    raise typer.Exit(code)


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
