"""S1 数据模型：TaskSpec（任务定义）与 JobRecord（作业记录）。

status 状态机：
    queued -> running -> (succeeded | failed | budget_exceeded | step_exceeded
                          | wallclock_exceeded | cancelled | stuck)
    running -> paused -> running

budget_exceeded / step_exceeded / wallclock_exceeded 为三种不同的资源耗尽态，
分别对应：成本超预算 / 步数用尽 / 墙钟到点（7×24 任务可配置为不限）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TaskBudget(BaseModel):
    """任务预算。0 值语义：wall_s/steps=0 表示未指定（执行时用 profile budget）；
    rounds/cost_cny=0 表示不限。显式传负数（如 wall_s=-1）表示强制不限。"""

    wall_s: float = 0.0  # 墙钟上限（秒），0=未指定（用 profile），<0=不限
    steps: int = 0  # 最大步数，0=未指定（用 profile），<0=不限
    rounds: int = 0  # 最大轮数，0 表示不限
    cost_cny: float = 0.0  # 成本上限（元），0 表示不限


class TaskSpec(BaseModel):
    """原始任务定义快照（落盘到 var/jobs/<job_id>/task.yaml）。"""

    job_id: str
    task_type: str = "generic"  # ctf | pentest | redteam | assessment | codeaudit | generic
    objective: str  # 任务全文，原文保存，不做摘要
    priority: int = 0  # 越大越优先
    prior: str | None = None  # 先验覆盖：knowledge/priors/<name>.md（如 saml）
    allow_hosts: list[str] = Field(default_factory=list)
    deny_hosts: list[str] = Field(default_factory=list)
    max_rps: float = 0.0  # 每秒最大请求数，0 表示不限
    budget: TaskBudget = Field(default_factory=TaskBudget)
    artifacts: list[str] = Field(default_factory=list)  # 只读附件路径/目录（绝对路径，已就位）
    entries: list[str] = Field(default_factory=list)  # S5 多入口：>=2 时走 DAG 编排


class JobRecord(BaseModel):
    """作业记录（对应 SQLite jobs 表一行）。"""

    job_id: str
    spec: TaskSpec
    status: str = "queued"  # queued|running|paused|succeeded|failed|budget_exceeded|cancelled|stuck
    round: int = 0  # 已完成的最大轮数
    steps: int = 0  # 已完成步数
    cost_cny: float = 0.0
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)
    last_error: str = ""

    def touch(self) -> None:
        self.updated_at = _utcnow()

    def set_status(self, status: str, *, error: str = "") -> None:
        self.status = status
        if error:
            self.last_error = error
        self.touch()
