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

import yaml
from pathlib import Path

from secpt.core.job.models import TaskSpec
from secpt.core.job.rounds import load_latest_round
from secpt.runtime.engine import RunConfig

# jobs 目录：默认项目根 var/jobs，可被环境变量 SECPT_VAR 覆盖
DEFAULT_JOBS_DIR = Path("var") / "jobs"


def jobs_dir() -> Path:
    import os
    return Path(os.environ.get("SECPT_VAR", "var")) / "jobs"


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
        max_steps=run_config["max_steps"] if "max_steps" in run_config else (spec.budget.steps or 100),
        max_wall_s=run_config["max_wall_s"] if "max_wall_s" in run_config else (int(spec.budget.wall_s) or 900),
        bash_timeout_s=run_config.get("bash_timeout_s", 60),
        bash_whitelist=frozenset(run_config.get("bash_whitelist")) if run_config.get("bash_whitelist") else DEFAULT_BASH_WHITELIST,
        flag_regex=run_config.get("flag_regex"),
        model_primary=run_config.get("model_primary", "deepseek-chat"),
        model_fallback=run_config.get("model_fallback", "deepseek-reasoner"),
        stuck_threshold=run_config.get("stuck_threshold", 6),
        stuck_increment=run_config.get("stuck_increment", 2),
        context_window=run_config.get("context_window", 64_000),
        budget_cost_cny=run_config.get("budget_cost_cny", spec.budget.cost_cny),
        round_max_steps=run_config.get("round_max_steps", 60),
        round_max_wall_s=run_config.get("round_max_wall_s", 1500),
    )
    return cfg
