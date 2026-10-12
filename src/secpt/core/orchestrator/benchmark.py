"""Benchmark 并行跑分器：一次三题，平台容器上限与并行度对齐。

设计（tsec-run-1150/1214 复盘驱动）：
- 平台同时允许 3 个活跃容器 → 我们固定 3 个并行 worker，每 worker 一题
- 每题是 frontier 的一条 intent，带**意图级步数预算（25 步硬止损）**——
  烧完自动判 exhausted 换下一题，把"单题止损纪律"从 prompt 升格为运行时强制
- 选题：平台 list 接口即断点（is_completed/correct_flag_count），按
  level 与难度排序，先拿简单分
- 工作区共享：脚本命名必须带题号前缀（goal 里写明，防并行互踩）

solve 在 benchmark profile 下自动走此路径（run_mode=single 且 profile=benchmark）。
"""

from __future__ import annotations

import json
import os
import urllib.request

from secpt.core.job.models import TaskSpec
from secpt.core.orchestrator.frontier import Frontier, run_frontier

DIFF_RANK = {"easy": 0, "medium": 1, "hard": 2}
PER_CHALLENGE_STEP_BUDGET = 25  # 单题硬止损（先验纪律的运行时强制）


def fetch_challenges(
    *, base: str | None = None, token: str | None = None, timeout_s: float = 20.0
) -> list[dict]:
    """拉题目列表（平台 list 即断点：correct_flag_count/is_completed）。"""
    base = (base or os.environ.get("BENCHMARK_BASE_URL", "")).rstrip("/")
    token = token or os.environ.get("BENCHMARK_TOKEN", "")
    if not base or not token:
        raise RuntimeError("BENCHMARK_BASE_URL / BENCHMARK_TOKEN 未设置（solve 预检应已拦截）")
    req = urllib.request.Request(
        f"{base}/openapi/v1/challenges",
        headers={"BENCHMARK_TOKEN": token},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        raise RuntimeError(f"题目列表格式异常: {str(data)[:200]}")
    return data


def _sorted_open(challenges: list[dict], *, limit: int = 60) -> list[dict]:
    """未完成题排序：level 升序 → 难度 easy→hard → 已得分多的先补完。"""
    open_ = [
        c
        for c in challenges
        if not c.get("is_completed") and c.get("container_status") != "available"
    ]
    open_.sort(
        key=lambda c: (
            int(c.get("level") or 9),
            DIFF_RANK.get(str(c.get("difficulty") or "").lower(), 3),
            -int(c.get("correct_flag_count") or 0),
        )
    )
    return open_[:limit]


BENCH_WORKER_OBJECTIVE = """【评测跑分循环 · 你的当前任务】
你在并行跑分队伍中（每 worker 一题，工作区共享——脚本文件名必须带本题 unique_code 前缀）。
对分配给你的这一道题，严格执行：start → 探测解题 → submit 每个 flag → close。
铁律（详见已注入的先验 benchmark.md）：只打 API 给的 container_addr；每题必 close；
超过 {budget} 步未出 flag 会被系统强制止损换题——你自己应先做决策（hint 或主动换题）。
完成后输出 ANSWER: <unique_code 结果：拿到几个 flag / 得分 / 卡点一句话>。
"""


async def run_benchmark(
    spec: TaskSpec,
    *,
    max_parallel: int = 3,
    llm=None,
    model: str | None = None,
    frontier: Frontier | None = None,
) -> dict:
    """并行跑分主入口：选题入队 → frontier 3 worker 并行。"""
    challenges = fetch_challenges()
    picked = _sorted_open(challenges)
    if not picked:
        return {
            "job_id": spec.job_id,
            "mode": "benchmark",
            "flags": [],
            "chains": [
                {
                    "intent": "（无未完成的题目）",
                    "status": "done",
                    "steps": 0,
                    "depth": 0,
                    "source": "planner",
                }
            ],
            "open_remaining": 0,
            "wall_s": 0.0,
        }

    fr = frontier or Frontier()
    for rank, c in enumerate(picked):
        uc = str(c.get("unique_code"))
        flags_done = int(c.get("correct_flag_count") or 0)
        flags_total = int(c.get("flag_count") or 1)
        fr.add(
            f"通关 {uc}（{c.get('difficulty')} L{c.get('level')}，flag {flags_done}/{flags_total}）",
            goal=(
                f"unique_code={uc}；description={str(c.get('description') or '')[:300]}；\n"
                f"已得分 {c.get('total_score')} 分题，剩余 {flags_total - flags_done} 个 flag。\n"
                f"先用 api.py start {uc} 拿 container_addr，解题后 submit，最后务必 close。\n"
                f"脚本命名带 {uc} 前缀（并行共享工作区，防互踩）。"
            ),
            done_criteria="submit 全部剩余 flag（或判定不可解并已 close）后以 ANSWER: 收尾",
            priority=100 - rank,
            depth=0,
            source="planner",
            max_steps=PER_CHALLENGE_STEP_BUDGET,
        )

    objective = BENCH_WORKER_OBJECTIVE.format(budget=PER_CHALLENGE_STEP_BUDGET)
    summary = await run_frontier(
        objective,
        profile="benchmark",
        base_job_id=spec.job_id,
        llm=llm,
        model=model,
        frontier=fr,
        max_workers=max_parallel,
        max_rounds=len(picked) + 5,
    )
    summary["mode"] = "benchmark(parallel)"
    summary["challenges_total"] = len(challenges)
    summary["challenges_picked"] = len(picked)
    return summary
