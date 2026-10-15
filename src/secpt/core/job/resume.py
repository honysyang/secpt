"""S1 resume：从磁盘重建作业并继续。

目录约定（var/jobs/<job_id>/）：
    task.yaml        # TaskSpec + run_config 快照（submit 时写入）
    events.jsonl     # append-only 事件流
    rounds/NNN.messages.json   # 每轮 messages 快照
    ws/              # 模型可写工作区
    artifacts/       # 只读附件
    report.md        # 增量报告（S6 才填满，这里先建空壳）

resume 流程：
    1. 读 task.yaml 恢复 spec + run_config
    2. load_latest_round 取最新 messages（空则从头开始）
    3. 以该 messages 作为 Engine 初始链继续（首条 system 直接复用，保 prefix cache）
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from secpt.core.job.models import TaskSpec
from secpt.runtime.engine import RunConfig

# jobs 目录：默认项目根 var/jobs，可被环境变量 SECPT_VAR 覆盖
DEFAULT_JOBS_DIR = Path("var") / "jobs"


def jobs_dir() -> Path:
    import os

    return Path(os.environ.get("SECPT_VAR", "var")) / "jobs"


def load_last_checkpoint(job_id: str, base: Path | None = None) -> dict | None:
    """事件驱动恢复：rounds 快照 + events.jsonl 重放，聚合出可续跑状态。

    返回 dict：
        round             最新 round 编号（session_round 接续）
        messages          最新完整消息链（含首条 system）
        prompt_tokens / completion_tokens / cost_cny   tokens 事件累计（预算恢复）
        nudges            user_msg(kind=nudge) 计数（detector.trigger_count 近似恢复）
        summary_k         已发生的压缩摘要数（compaction/…事件最大 k + 1）
        steps             已执行步数（step 事件最大 n，防重启绕过 max_steps）

    无消息快照或无事件流 → None（首次运行）。
    """
    jd = (Path(base) if base else jobs_dir()) / job_id
    events_path = jd / "events.jsonl"
    from secpt.core.job.rounds import load_latest_round

    rnd, msgs = load_latest_round(job_id, base or jobs_dir())
    if not msgs or not events_path.is_file():
        return None

    prompt = completion = 0
    cost = 0.0
    nudges = 0
    summary_k = -1
    steps = 0
    with open(events_path, encoding="utf-8") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # 半截行（断电窗口）跳过
            t = ev.get("type")
            pl = ev.get("payload") or {}
            if t == "tokens":
                prompt += int(pl.get("prompt") or 0)
                completion += int(pl.get("completion") or 0)
                cost += float(pl.get("cost_cny") or 0.0)
            elif t == "user_msg" and pl.get("kind") == "nudge":
                nudges += 1
            elif t in ("compaction", "compaction_requested", "compaction_completed"):
                summary_k = max(summary_k, int(pl.get("k") or 0))
            elif t == "step":
                steps = max(steps, int(pl.get("n") or 0))

    if cost == 0.0 and (prompt or completion):
        # 旧日志无 tokens 事件时按当前单价估算（合理近似）
        from secpt.core.context.budget import tokens_cost

        cost = tokens_cost(prompt, completion)

    return {
        "round": rnd,
        "messages": msgs,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cost_cny": round(cost, 6),
        "nudges": nudges,
        "summary_k": summary_k + 1,
        "steps": steps,
    }


def write_task_snapshot(job_dir: Path, spec: TaskSpec, run_config: dict) -> None:
    """submit 时写入 task.yaml 快照。"""
    job_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "spec": spec.model_dump(mode="json"),
        "run_config": run_config,
    }
    (job_dir / "task.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # artifacts/ 与 report.md 空壳
    (job_dir / "artifacts").mkdir(exist_ok=True)
    report = job_dir / "report.md"
    if not report.exists():
        report.write_text(f"# {spec.job_id}\n\n任务状态：进行中\n", encoding="utf-8")


def read_task_snapshot(job_dir: Path) -> tuple[TaskSpec, dict]:
    """读 task.yaml，返回 (spec, run_config)。"""
    data = yaml.safe_load((job_dir / "task.yaml").read_text(encoding="utf-8"))
    return TaskSpec.model_validate(data["spec"]), data.get("run_config") or {}


def make_run_config(job_id: str, spec: TaskSpec, run_config: dict) -> RunConfig:
    """由快照重建 RunConfig（resume 用）。"""
    from secpt.runtime.engine import DEFAULT_BASH_WHITELIST

    cfg = RunConfig(
        job_id=job_id,
        objective=spec.objective,
        system_extra="",  # resume 时首条 system 直接来自初始链，不需重建
        workspace=Path(run_config.get("workspace") or jobs_dir() / job_id),
        max_steps=run_config.get("max_steps", spec.budget.steps or 100),
        max_wall_s=run_config.get("max_wall_s", int(spec.budget.wall_s) or 900),
        bash_timeout_s=run_config.get("bash_timeout_s", 60),
        bash_whitelist=frozenset(run_config.get("bash_whitelist"))
        if run_config.get("bash_whitelist")
        else DEFAULT_BASH_WHITELIST,
        flag_regex=run_config.get("flag_regex"),
        model_primary=run_config.get("model_primary", "deepseek-chat"),
        model_fallback=run_config.get("model_fallback", "deepseek-reasoner"),
        stuck_threshold=run_config.get("stuck_threshold", 6),
        stuck_increment=run_config.get("stuck_increment", 2),
        stuck_wait_cap=run_config.get("stuck_wait_cap", 12),
        context_window=run_config.get("context_window", 64_000),
        budget_cost_cny=run_config.get("budget_cost_cny", spec.budget.cost_cny),
        round_max_steps=run_config.get("round_max_steps", 60),
        round_max_wall_s=run_config.get("round_max_wall_s", 1500),
        sandbox=run_config.get("sandbox", "none"),
    )
    return cfg
