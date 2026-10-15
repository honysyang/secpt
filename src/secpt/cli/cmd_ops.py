"""人在环路运维命令：直投意图 / 操舵 / 拦截审批 / 指标 / 图导出。

ARTEX 三种工作模式在 CLI 上的落点：
- 自主探索   → secpt solve / hunt（默认）
- 人工直投   → secpt intent add（"人点单、AI 干活"，规划者自动暂停）
- 人在环路   → secpt steer（中途纠偏）+ secpt intercept/approve/deny（关键节点把关）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

intent_app = typer.Typer(help="直投意图：人点单、AI 干活（规划者自动暂停）")
intercept_app = typer.Typer(help="拦截审批规则管理")
worker_app = typer.Typer(help="后台 worker：摘要补偿 / 指标端点")


# ---------------------------------------------------------------------------
# 后台 worker
# ---------------------------------------------------------------------------


@worker_app.command("summarize")
def worker_summarize(
    job_id: str = typer.Argument(None, help="限定 job_id；缺省扫描全部 job"),
) -> None:
    """补偿处理 compaction/pending 残留摘要请求（引擎崩溃后兜底）。"""
    from secpt.core.job.summarize_worker import run_summarize_worker

    raise typer.Exit(run_summarize_worker(job_id))


@worker_app.command("metrics")
def worker_metrics(
    port: int = typer.Option(9100, "--port", "-p", help="监听端口"),
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
) -> None:
    """启动 Prometheus 文本格式指标端点（GET /metrics）。"""
    import time as _time

    from secpt.runtime.metrics import start_metrics_server

    start_metrics_server(port, host)
    typer.echo(f"metrics 端点已启动: http://{host}:{port}/metrics （Ctrl-C 退出）")
    try:
        while True:
            _time.sleep(1)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# 直投意图
# ---------------------------------------------------------------------------


@intent_app.command("add")
def intent_add(
    title: str = typer.Argument(..., help="意图标题（要测什么）"),
    goal: str = typer.Option("", "--goal", "-g", help="目标说明"),
    done: str = typer.Option("", "--done", "-d", help="完成判据"),
    priority: int = typer.Option(9, "--priority", "-p", help="优先级（默认 9 插队）"),
) -> None:
    """往当前 frontier 队列直投一条意图（source=direct，planner 见 direct 即暂停）。"""
    from secpt.core.orchestrator.frontier import Frontier

    fr = Frontier()
    iid = fr.add(title, goal=goal, done_criteria=done, priority=priority, source="direct")
    typer.echo(
        f"已直投意图 {iid}（priority={priority}）。运行中的 hunt 将在当前意图后执行它；"
        f"无运行中的 hunt 时启动 `secpt hunt` 即只跑 direct 意图。"
    )


@intent_app.command("list")
def intent_list(
    status: str | None = typer.Option(None, "--status", "-s", help="open/running/done/exhausted"),
) -> None:
    """查看意图队列（★=direct 人工直投，#=planner）。"""
    from secpt.core.orchestrator.frontier import Frontier

    rows = Frontier().list_all(status=status)
    if not rows:
        typer.echo("（队列为空）")
        return
    typer.echo(f"{'id':14s} {'源':6s} {'状态':10s} {'prio':>4s} 标题")
    for r in rows:
        tag = "★人" if r.get("source") == "direct" else "#AI"
        typer.echo(
            f"{r['id']:14s} {tag:6s} {r['status']:10s} {r['priority']:>4d} {r['title'][:50]}"
        )


# ---------------------------------------------------------------------------
# Steer 操舵
# ---------------------------------------------------------------------------


def steer_cmd(
    job_id: str = typer.Argument(..., help="目标 job_id"),
    msg: str = typer.Argument(..., help="操舵提示（下一轮即注入，不打断任务）"),
) -> None:
    """中途纠偏：把提示注入正在运行的任务（ARTEX SteerWork 同款）。"""
    from secpt.core.orchestrator.steer import SteerBox

    mid = SteerBox().push(job_id, msg)
    typer.echo(f"已注入操舵消息 #{mid} → job {job_id}（该任务下一步即可看到）")


# ---------------------------------------------------------------------------
# 拦截审批
# ---------------------------------------------------------------------------


@intercept_app.command("rules")
def intercept_rules() -> None:
    """列出拦截规则（★=内置种子，可停用不可删；自定义规则可删）。"""
    from secpt.governance.intercept import Interceptor

    rows = Interceptor().list_rules()
    typer.echo(f"{'id':>3s} {'prio':>4s} {'动作':6s} {'目标':8s} {'启用':4s} 名称 / 模式")
    for r in rows:
        star = "★" if r.get("builtin") else " "
        typer.echo(
            f"{star}{r['id']:>2d} {r['priority']:>4d} {r['action']:6s}"
            f" {r['match_target']:8s} {'✓' if r['enabled'] else '✗':4s}"
            f" {r['name']}  [{r['pattern'][:60]}]"
        )


@intercept_app.command("add")
def intercept_add(
    name: str = typer.Option(..., "--name"),
    pattern: str = typer.Option(..., "--pattern", help="正则"),
    action: str = typer.Option(..., "--action", help="allow|deny|ask"),
    priority: int = typer.Option(5, "--priority"),
    message: str = typer.Option("", "--message"),
    timeout_s: int = typer.Option(300, "--timeout"),
) -> None:
    """新增拦截规则（action=ask 时阻塞等待人工审批）。"""
    from secpt.governance.intercept import Interceptor

    rid = Interceptor().add_rule(
        name=name,
        pattern=pattern,
        action=action,
        priority=priority,
        message=message,
        timeout_s=timeout_s,
    )
    typer.echo(f"已新增规则 #{rid}")


@intercept_app.command("toggle")
def intercept_toggle(
    rule_id: int = typer.Argument(...),
    enable: bool = typer.Option(True, "--enable/--disable"),
) -> None:
    from secpt.governance.intercept import Interceptor

    Interceptor().set_enabled(rule_id, enable)
    typer.echo(f"规则 #{rule_id} {'启用' if enable else '停用'}")


def pending_cmd() -> None:
    """查看等待人工审批的拦截请求。"""
    from secpt.governance.intercept import Interceptor

    rows = Interceptor().pending_list()
    if not rows:
        typer.echo("（无待审批）")
        return
    for r in rows:
        typer.echo(f"⏸ #{r['id']} [{r['rule_name']}] job={r['job_id'] or '-'}")
        typer.echo(f"   命令: {r['preview'][:120]}")
        typer.echo(f"   操作: secpt approve {r['id']}  或  secpt deny {r['id']}")


def decide_cmd(pending_id: str, approve: bool) -> None:
    """审批：secpt approve <id> / secpt deny <id>。"""
    from secpt.governance.intercept import Interceptor

    if Interceptor().decide(pending_id, approve):
        typer.echo(f"#{pending_id} 已{'放行' if approve else '拒绝'}")
    else:
        typer.echo(f"Error: #{pending_id} 不存在或已被处理")
        raise typer.Exit(2)


# ---------------------------------------------------------------------------
# 指标（D3.1）
# ---------------------------------------------------------------------------


METRICS_HISTORY = "metrics_history.jsonl"


def _history_path() -> Path:
    return Path(os.environ.get("SECPT_VAR", "var")) / METRICS_HISTORY


def collect_metrics(*, _job_store=None, _finding_store=None) -> dict:
    """聚合运营指标（metrics/report/snapshot/trend 共用）。"""
    from secpt.core.job.store import JobStore
    from secpt.findings.store import FindingStore

    store = _job_store or JobStore()
    jobs = store.list_jobs(limit=10000)
    total = len(jobs)
    succeeded = sum(1 for j in jobs if j.status == "succeeded")
    cancelled = sum(1 for j in jobs if j.status == "cancelled")
    budgetish = sum(
        1 for j in jobs if j.status in ("budget_exceeded", "step_exceeded", "wallclock_exceeded")
    )
    resumed = sum(1 for j in jobs if j.round > 0)
    cost = sum(j.cost_cny for j in jobs)
    steps = [j.steps for j in jobs if j.steps > 0]

    fs = _finding_store or FindingStore()
    all_f = fs.list(limit=10000)
    confirmed = sum(1 for f in all_f if f.status == "confirmed")
    fp = sum(1 for f in all_f if f.status == "false_positive")
    inferred = sum(1 for f in all_f if f.status == "inferred")
    reproduced = sum(1 for f in all_f for r in f.reproduced_by if r.get("verdict") == "reproduced")
    fixed = sum(1 for f in all_f for r in f.reproduced_by if r.get("verdict") == "fixed")

    n_steer = 0
    n_approval = 0
    try:
        import sqlite3

        var = Path(os.environ.get("SECPT_VAR", "var"))
        sp = var / "steer.sqlite"
        if sp.is_file():
            with sqlite3.connect(str(sp)) as c:
                n_steer = c.execute("SELECT COUNT(*) FROM steer_msgs").fetchone()[0]
        ipath = var / "intercept.sqlite"
        if ipath.is_file():
            with sqlite3.connect(str(ipath)) as c:
                n_approval = c.execute(
                    "SELECT COUNT(*) FROM intercept_pending WHERE status!='pending'"
                ).fetchone()[0]
    except sqlite3.Error:
        pass

    return {
        "tasks": {
            "total": total,
            "succeeded": succeeded,
            "cancelled": cancelled,
            "resource_exhausted": budgetish,
            "resumed_after_interrupt": resumed,
            "success_rate": round(succeeded / total, 3) if total else 0.0,
            "total_cost_cny": round(cost, 4),
            "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0.0,
        },
        "findings": {
            "total": len(all_f),
            "confirmed": confirmed,
            "inferred": inferred,
            "false_positive": fp,
            "fp_rate": round(fp / (confirmed + fp), 3) if (confirmed + fp) else 0.0,
            "retest_reproduced": reproduced,
            "retest_fixed": fixed,
        },
        "human_loop": {"steer_msgs": n_steer, "approvals": n_approval},
    }


def _render_metrics(data: dict) -> str:
    t = data["tasks"]
    f = data["findings"]
    h = data["human_loop"]
    lines = [
        "== 任务 ==",
        f"  总数 {t['total']}  成功 {t['succeeded']} ({t['success_rate'] * 100:.1f}%)"
        f"  资源耗尽 {t['resource_exhausted']}  中断后续跑 {t['resumed_after_interrupt']}",
        f"  平均步数 {t['avg_steps']}  累计成本 ¥{t['total_cost_cny']}",
        "== 发现 ==",
        f"  总数 {f['total']}  confirmed {f['confirmed']}  inferred {f['inferred']}"
        f"  误报率 {f['fp_rate'] * 100:.1f}%  复现成功 {f['retest_reproduced']} 已修复 {f['retest_fixed']}",
        "== 人在环路 ==",
        f"  操舵 {h['steer_msgs']} 次  审批 {h['approvals']} 次",
    ]
    return "\n".join(lines)


def metrics_cmd(
    fmt: str = "table",
    snapshot: bool = False,
    trend: int = 0,
    *,
    _job_store=None,
    _finding_store=None,
) -> None:
    """运营指标：成功率/确认率/误报率/成本/干预次数/恢复能力（ARTEX 原则 10）。

    --snapshot 记录历史；--trend N 打印与最近 N 次快照的关键指标增量。
    """
    import time as _time

    data = collect_metrics(_job_store=_job_store, _finding_store=_finding_store)
    if fmt == "json":
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        typer.echo(_render_metrics(data))

    hp = _history_path()
    if trend > 0:
        history: list[dict] = []
        if hp.is_file():
            history = [
                json.loads(line)
                for line in hp.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if not history:
            typer.echo("（无历史快照；先 --snapshot 记录几轮）")
        else:
            base = history[-trend]
            typer.echo(f"\n== 趋势（相对 {-trend} 次前）==")
            for dim, keys in (
                ("tasks", ("total", "succeeded", "success_rate", "total_cost_cny")),
                ("findings", ("total", "confirmed", "fp_rate")),
            ):
                for k in keys:
                    delta = data[dim].get(k, 0) - base["data"].get(dim, {}).get(k, 0)
                    sign = "+" if delta > 0 else ""
                    typer.echo(
                        f"  {dim}.{k}: {base['data'].get(dim, {}).get(k, 0)} → "
                        f"{data[dim].get(k, 0)}（{sign}{round(delta, 3) if isinstance(delta, float) else delta}）"
                    )
    if snapshot:
        hp.parent.mkdir(parents=True, exist_ok=True)
        with open(hp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _time.time(), "data": data}, ensure_ascii=False) + "\n")
        typer.echo(f"（已记录快照 → {hp}）")
