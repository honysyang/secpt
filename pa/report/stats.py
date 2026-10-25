"""secpt stats：跨 mission 聚合统计（S13）。只读各目录的 events.jsonl。

每 mission 一行：steps / tokens / tasks(done,failed,blocked) /
virtual_success / authz / 峰值 no_progress / 墙钟 / 终态。
聚合段：virtual_success 率、AuthZ 占比、attempt 分布、worker 步数分布
（来自 context.assembled 事件）、prompts 尺寸斜率（同样来自
context.assembled 的 total_chars 序列做最小二乘）。

末尾「调参建议」：每个越线指标给一句可执行建议。阈值是常量占位——
等真实数据回来再标定，这里只留接口（suggest() 纯函数，可单测）。
"""
from __future__ import annotations

import json
from pathlib import Path

from pa.errors import SecptError
from pa.store.view import StoreView

# 调参建议阈值（占位，待真实数据标定）
VS_RATE_WARN = 0.2          # virtual_success 率 > 20%
AUTHZ_RATIO_WARN = 0.3      # AuthZ 阻塞占全部阻塞 > 30%
ATTEMPT3_WARN = 0.5         # 打满 MAX_ATTEMPTS 的任务占比 > 50%
SLOPE_WARN = 2000.0         # prompts 每步增长 > 2000 字符

_AUTHZ_REASONS = ("out_of_scope", "needs_approval")


def collect(root) -> dict:
    """读单个 mission 账本，产出指标 dict。"""
    root = Path(root)
    events = list(StoreView(root).events())
    if not events:
        raise SecptError(f"无事件账本: {root}")
    steps = 0
    tokens = 0
    task_state: dict[str, str] = {}
    task_attempt: dict[str, int] = {}
    vs_tasks: set[str] = set()            # vs_task_rate 分子：按任务去重
    vs_events = 0                         # vs_events：按事件计（教练口径）
    unattributed = 0                      # 旧 mission 动作无 task_id，不回溯猜测
    authz = 0
    blocked = 0
    peak_np = 0
    np_run = 0
    saw_clue = False
    ctx_sizes: list[int] = []
    worker_runs: dict[tuple[str, int], int] = {}
    fin = "未完成"
    wall = int((events[-1].ts - events[0].ts).total_seconds())
    for ev in events:
        t, p = ev.type, ev.payload
        if t == "task.created":
            task_state[p["task"]["id"]] = "pending"
            task_attempt[p["task"]["id"]] = 0
        elif t == "task.state_changed":
            tk = p.get("task_id")
            if tk in task_state:
                task_state[tk] = str(p.get("to"))
            if p.get("attempt") is not None and tk in task_attempt:
                task_attempt[tk] = int(p["attempt"])
            if p.get("to") == "failed" and p.get("reason") == "virtual_success":
                vs_events += 1
                vs_tasks.add(str(tk))
        elif t in ("action.started", "action.finished", "action.blocked") \
                and not p.get("task_id"):
            unattributed += 1             # 裁定 2：只计数，不归属
        elif t == "budget.charged":
            steps += 1
            tokens += int(p.get("tokens", 0))
            np_run = 0 if saw_clue else np_run + 1
            peak_np = max(peak_np, np_run)
            saw_clue = False
        elif t == "clue.added":
            saw_clue = True
        elif t == "action.blocked":
            blocked += 1
            if p.get("reason") in _AUTHZ_REASONS:
                authz += 1
        elif t == "context.assembled":
            ctx_sizes.append(int(p.get("total_chars", 0)))
            if p.get("phase") == "worker":      # planner 段不混入 worker 步数
                key = (str(p.get("task_id")), int(p.get("attempt", 0)))
                worker_runs[key] = worker_runs.get(key, 0) + 1
        elif t == "mission.finished":
            fin = str(p.get("reason", "未完成"))
    counts: dict[str, int] = {}
    for s in task_state.values():
        counts[s] = counts.get(s, 0) + 1
    attempts_hist: dict[int, int] = {}
    for a in task_attempt.values():
        attempts_hist[a] = attempts_hist.get(a, 0) + 1
    worker_hist: dict[int, int] = {}
    for n in worker_runs.values():
        worker_hist[n] = worker_hist.get(n, 0) + 1
    return {
        "name": root.name,
        "steps": steps, "tokens": tokens, "wall_s": wall,
        "tasks": counts, "vs_events": vs_events,
        "vs_tasks": len(vs_tasks),
        "actions_unattributed": unattributed,
        "authz": authz, "blocked": blocked,
        "peak_no_progress": peak_np,
        "attempt_dist": dict(sorted(attempts_hist.items())),
        "worker_step_dist": dict(sorted(worker_hist.items())),
        "prompt_slope": _slope(ctx_sizes),
        "terminal": fin,
    }


def _slope(ys: list[float]) -> float:
    """最小二乘斜率：x = 0..n-1（prompt 尺寸随步序的增长率）。"""
    n = len(ys)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(ys) / n
    num = sum((i - mx) * (y - my) for i, y in enumerate(ys))
    den = sum((i - mx) ** 2 for i in range(n))
    return round(num / den, 2) if den else 0.0


def aggregate(rows: list[dict]) -> dict:
    total_tasks = sum(sum(r["tasks"].values()) for r in rows)
    total_vs_events = sum(r["vs_events"] for r in rows)
    total_vs_tasks = sum(r["vs_tasks"] for r in rows)
    total_blocked = sum(r["blocked"] for r in rows)
    total_authz = sum(r["authz"] for r in rows)
    attempt_dist: dict[int, int] = {}
    worker_dist: dict[int, int] = {}
    for r in rows:
        for k, v in r["attempt_dist"].items():
            attempt_dist[k] = attempt_dist.get(k, 0) + v
        for k, v in r["worker_step_dist"].items():
            worker_dist[k] = worker_dist.get(k, 0) + v
    slopes = [r["prompt_slope"] for r in rows]
    return {
        "missions": len(rows),
        # 裁定 1：双口径分开命名，禁止同名字段混用
        "vs_events": total_vs_events,                       # 按事件计（教练用）
        "vs_task_rate": round(total_vs_tasks / total_tasks, 4)
        if total_tasks else 0.0,                            # 按任务去重 0~1（报告用）
        "authz_ratio": round(total_authz / total_blocked, 4) if total_blocked else 0.0,
        "attempt_dist": dict(sorted(attempt_dist.items())),
        "worker_step_dist": dict(sorted(worker_dist.items())),
        "mean_prompt_slope": round(sum(slopes) / len(slopes), 2) if slopes else 0.0,
        "attempt3_ratio": round(
            attempt_dist.get(3, 0) / total_tasks, 4) if total_tasks else 0.0,
    }


def suggest(agg: dict) -> list[str]:
    """每个越线指标一句可执行建议。阈值是模块常量（占位待标定）。"""
    out: list[str] = []
    if agg["vs_task_rate"] > VS_RATE_WARN:
        out.append(
            f"vs_task_rate {agg['vs_task_rate']:.0%} 越线"
            f"（>{VS_RATE_WARN:.0%}，vs_events={agg['vs_events']}）："
            f"任务判据写得太松，worker 报 done 但证据不满足——"
            f"收紧 success_criteria 的 evidence 指向。")
    if agg["authz_ratio"] > AUTHZ_RATIO_WARN:
        out.append(
            f"AuthZ 阻塞占比 {agg['authz_ratio']:.0%} 越线"
            f"（>{AUTHZ_RATIO_WARN:.0%}）：计划与范围/工具白名单脱节——"
            f"在 planner 提示词里强调 allowed_scope 与 allowed_tools。")
    if agg["attempt3_ratio"] > ATTEMPT3_WARN:
        out.append(
            f"打满 3 次尝试的任务占比 {agg['attempt3_ratio']:.0%} 越线"
            f"（>{ATTEMPT3_WARN:.0%}）：检查 Model 失败详情与工具可用性，"
            f"必要时给 probe 类工具加超时预算。")
    if agg["mean_prompt_slope"] > SLOPE_WARN:
        out.append(
            f"prompts 平均每步增长 {agg['mean_prompt_slope']:.0f} 字符越线"
            f"（>{SLOPE_WARN:.0f}）：线索窗口或 untrusted 缓冲有泄漏——"
            f"用 secpt trace --task 定位 context.assembled 里持续增长的段。")
    return out


# ─────────────────────────── 文本渲染（确定性） ───────────────────────────

def render_table(rows: list[dict]) -> str:
    head = (f"{'MISSION':<24} {'STEP':>4} {'TOKENS':>7} {'TASKS(d/f/b)':>12} "
            f"{'VS(e/t)':>8} {'AUTHZ':>5} {'NP峰':>4} {'墙钟':>5} {'未归属':>4}  终态")
    lines = [head, "-" * len(head)]
    for r in rows:
        t = r["tasks"]
        dfb = f"{t.get('done', 0)}/{t.get('failed', 0)}/{t.get('blocked', 0)}"
        vst = f"{r['vs_events']}/{r['vs_tasks']}"
        lines.append(
            f"{r['name']:<24} {r['steps']:>4} {r['tokens']:>7} {dfb:>12} "
            f"{vst:>8} {r['authz']:>5} {r['peak_no_progress']:>4} "
            f"{r['wall_s']:>4}s {r['actions_unattributed']:>4}  {r['terminal']}")
    return "\n".join(lines)


def render(rows: list[dict]) -> str:
    agg = aggregate(rows)
    lines = ["# secpt stats", "", render_table(rows), "",
             "## 聚合", "",
             f"missions: {agg['missions']}",
             f"vs_events（按事件，教练口径）: {agg['vs_events']}",
             f"vs_task_rate（按任务去重，报告口径）: {agg['vs_task_rate']:.1%}",
             f"AuthZ 占比: {agg['authz_ratio']:.1%}",
             f"attempt 分布: {_hist(agg['attempt_dist'])}",
             f"worker 步数分布: {_hist(agg['worker_step_dist'])}",
             f"prompts 尺寸斜率(均值): {agg['mean_prompt_slope']:.2f} 字符/步",
             "", "## 调参建议", ""]
    tips = suggest(agg)
    lines += [f"- {s}" for s in tips] if tips else ["- 无（所有指标在阈值内）"]
    return "\n".join(lines) + "\n"


def render_json(rows: list[dict]) -> str:
    return json.dumps({"missions": rows, "aggregate": aggregate(rows)},
                      ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _hist(h: dict) -> str:
    if not h:
        return "（无数据）"
    return "、".join(f"{k} 步×{v} 任务" for k, v in sorted(h.items()))
