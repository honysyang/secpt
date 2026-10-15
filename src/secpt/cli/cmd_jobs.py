"""作业管理命令实现（命令注册在 cli/app.py）。

- jobs / ls / status：作业队列视图
- submit / cancel / forever：提交、取消、常驻 manager
- tail：事件流回放
"""

from __future__ import annotations

import json

import typer


def _job_dict(r) -> dict:
    """JobRecord → 可 JSON 序列化的精简 dict（--json 输出用）。"""
    return {
        "job_id": r.job_id,
        "status": r.status,
        "steps": r.steps,
        "round": r.round,
        "cost_cny": r.cost_cny,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "error": r.last_error or "",
        "objective": (r.spec.objective if r.spec else "")[:200],
    }


def jobs_cmd(
    status: str | None = typer.Option(None, "--status", "-s", help="按状态过滤"),
    as_json: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出 var/jobs/ 下的作业。"""
    from secpt.core.job.store import JobStore

    store = JobStore()
    rows = store.list_jobs(status=status)
    if as_json:
        typer.echo(json.dumps([_job_dict(r) for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        typer.echo("（无作业）")
        return
    typer.echo(f"{'job_id':12s} {'status':16s} {'steps':>5s} {'round':>5s}  created")
    for r in rows:
        typer.echo(
            f"{r.job_id:12s} {r.status:16s} {r.steps:>5d} {r.round:>5d}  {r.created_at[:19]}"
        )


def submit_cmd(
    task: str = typer.Option(..., "--task", "-t", help="任务 yaml 文件路径"),
    max_parallel: int = typer.Option(4, "--max-parallel", "-j", help="并发上限"),
    priority: int = typer.Option(0, "--priority", "-P", help="调度优先级（大者先出队；同级 FIFO）"),
) -> None:
    """提交任务到作业队列（TaskSpec yaml）。"""
    from pathlib import Path

    import yaml

    from secpt.core.job.manager import JobManager
    from secpt.core.job.models import TaskSpec

    data = yaml.safe_load(Path(task).read_text(encoding="utf-8"))
    spec = TaskSpec.model_validate(data)
    mgr = JobManager(max_parallel_jobs=max_parallel)
    mgr.submit(spec, {"priority": priority})
    typer.echo(f"已提交: {spec.job_id} (task_type={spec.task_type}, priority={priority})")
    mgr.reap()


def ls_cmd(
    status: str | None = typer.Option(None, "--status", "-s", help="按状态过滤"),
    max_parallel: int = typer.Option(4, "--max-parallel", "-j", help="并发上限"),
    as_json: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """列出作业（状态/步数/耗时）。"""
    from secpt.core.job.manager import JobManager

    mgr = JobManager(max_parallel_jobs=max_parallel)
    mgr.reap()
    rows = mgr.store.list_jobs(status=status)
    if as_json:
        typer.echo(json.dumps([_job_dict(r) for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        typer.echo("（无作业）")
        return
    typer.echo(f"{'job_id':12s} {'status':16s} {'steps':>5s} {'round':>5s}  created")
    for r in rows:
        typer.echo(
            f"{r.job_id:12s} {r.status:16s} {r.steps:>5d} {r.round:>5d}  {r.created_at[:19]}"
        )


def status_cmd(
    job_id: str = typer.Argument(..., help="job_id"),
    as_json: bool = typer.Option(False, "--json", help="机器可读输出"),
) -> None:
    """查看单个 job 详情。"""
    from secpt.core.job.manager import JobManager

    mgr = JobManager()
    mgr.reap()
    rec = mgr.get(job_id)
    if rec is None:
        typer.echo(f"Error: job {job_id} 不存在", err=True)
        raise typer.Exit(2)
    if as_json:
        typer.echo(json.dumps(_job_dict(rec), ensure_ascii=False, indent=2))
        return
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


def forever_cmd(
    max_parallel: int = typer.Option(4, "--max-parallel", "-j", help="并发上限"),
) -> None:
    """常驻 manager：轮询回收 + stuck 检测 + 重启恢复排队。Ctrl-C 退出。"""
    from secpt.core.job.manager import JobManager

    # 加强⑥：重启时从 jobs 表恢复 queued（优先级从 task.yaml 快照读回），
    # 常驻进程崩溃重启不丢排队任务
    mgr = JobManager(max_parallel_jobs=max_parallel, restore_queue=True)
    typer.echo(f"[manager] 常驻运行中，并发上限 {mgr.max_parallel_jobs}（Ctrl-C 退出）")
    try:
        mgr.run_forever()
    except KeyboardInterrupt:
        typer.echo("\n[manager] 退出")
