from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────── 任务 ───────────────────────────

TaskState = Literal[
    "pending", "ready", "running", "done", "failed", "blocked", "superseded"
]
RiskTier = Literal["low", "med", "high", "critical"]


class Criterion(BaseModel):
    """完成判据：指向证据类型，不接受形容词。"""
    evidence: str
    required: bool = True


class Precondition(BaseModel):
    kind: Literal["clue"] = "clue"
    match: dict[str, Any]


class Budget(BaseModel):
    steps: int = 12
    seconds: int = 180
    tokens: int = 40_000
    risk: RiskTier = "low"


class Lease(BaseModel):
    owner: str
    expires: datetime


class Lineage(BaseModel):
    supersedes: str | None = None
    superseded_by: list[str] = Field(default_factory=list)


class Task(BaseModel):
    id: str
    mission_id: str
    type: str
    goal: str
    parent: str | None = None
    deps: list[str] = Field(default_factory=list)
    preconditions: list[Precondition] = Field(default_factory=list)
    success_criteria: list[Criterion]
    abort_criteria: list[str] = Field(default_factory=list)
    scope_slice: dict[str, Any]
    allowed_tools: list[str]
    budget: Budget = Field(default_factory=Budget)
    priority: float = 0.5
    state: TaskState = "pending"
    lease: Lease | None = None
    attempt: int = 0
    # 进入 blocked 的原因（如 dangling_dep，裁定 3），随 task.state_changed
    # 落盘，重放还原
    block_reason: str | None = None
    tried: list[str] = Field(default_factory=list)
    lineage: Lineage = Field(default_factory=Lineage)


class TaskDraft(BaseModel):
    """LLM 产出的任务草案，id/mission_id 由引擎补。"""
    type: str
    goal: str
    parent: str | None = None
    deps: list[str] = Field(default_factory=list)
    preconditions: list[Precondition] = Field(default_factory=list)
    success_criteria: list[Criterion]
    abort_criteria: list[str] = Field(default_factory=list)
    scope_slice: dict[str, Any]
    allowed_tools: list[str]
    budget: Budget = Field(default_factory=Budget)
    priority: float = 0.5


# ─────────────────────────── 决策 ───────────────────────────

class CallTool(BaseModel):
    kind: Literal["call_tool"] = "call_tool"
    tool: str
    args: dict[str, Any]
    expect: str                      # 预期观察，必填，反思的锚点
    rationale: str = ""


class PlanOp(BaseModel):
    op: Literal["add_task", "drop_task", "change_priority", "supersede"]
    target: str | None = None
    task: TaskDraft | None = None
    priority: float | None = None


class Spawn(BaseModel):
    kind: Literal["spawn"] = "spawn"
    tasks: list[TaskDraft]
    rationale: str = ""


class Replan(BaseModel):
    kind: Literal["replan"] = "replan"
    ops: list[PlanOp]
    rationale: str = ""


class Wait(BaseModel):
    kind: Literal["wait"] = "wait"
    job_id: str
    timeout_s: int = 300


class Finish(BaseModel):
    kind: Literal["finish"] = "finish"
    summary: str
    satisfied: list[str] = Field(default_factory=list)   # acceptance id
    rationale: str = ""


Decision = Annotated[
    Union[CallTool, Spawn, Replan, Wait, Finish],
    Field(discriminator="kind"),
]


# ─────────────────────────── 结果 ───────────────────────────

FailureClass = Literal["Env", "Tool", "Model", "Plan", "AuthZ", "Deception"]
WorkerStatus = Literal["done", "partial", "failed", "blocked", "needs_help"]


class Clue(BaseModel):
    kind: str                        # host / port / service / path / cred / error
    key: str
    value: Any
    evidence_id: str | None = None
    confidence: float = 0.8
    source_task: str | None = None
    # 验收三要素之对象绑定（P1-2）：probe/http 线索必须带主机；path 仅
    # http 类线索填充（None = 不区分路径）。shell/fs 类无对象线索保持 None。
    target: str | None = None
    path: str | None = None


class FindingProposal(BaseModel):
    claim: str
    severity: Literal["info", "low", "med", "high", "critical"]
    evidence_refs: list[str]
    reproduce: str
    falsify_if: list[str]            # 必填：什么情况下这条不成立
    asset_id: str | None = None


class Failure(BaseModel):
    cls: FailureClass
    detail: str
    evidence_ref: str | None = None


class WorkerResult(BaseModel):
    task_id: str
    status: WorkerStatus
    clues: list[Clue] = Field(default_factory=list)
    findings: list[FindingProposal] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    tried: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    cost: dict[str, int] = Field(default_factory=dict)
    proposals: list[TaskDraft] = Field(default_factory=list)
    failure: Failure | None = None


# ─────────────────────────── 裁决 ───────────────────────────

Verdict = Literal["confirmed", "suspected", "false_positive"]


class Adjudication(BaseModel):
    finding_id: str
    verdict: Verdict
    verdict_by: str = "jev-v0"
    reason: str
    counter_evidence: list[str] = Field(default_factory=list)
    unlock: str = ""


# ─────────────────────────── 事件 ───────────────────────────

EventType = Literal[
    "mission.created", "mission.finished",
    "plan.created", "plan.revised",
    "task.created", "task.state_changed", "task.dispatched",
    "decision.made", "action.started", "action.finished", "action.blocked",
    "evidence.stored", "clue.added", "finding.proposed", "finding.adjudicated",
    "worker.returned", "mentor.intervened", "mentor.advisory",
    "mentor.advisory_agg", "restart.decided",
    "budget.charged", "budget.warning", "human.directive", "model.switched",
    "context.assembled",
]


class Event(BaseModel):
    seq: int
    mission_id: str
    ts: datetime = Field(default_factory=now)
    type: EventType
    actor: str                       # "engine" / "core" / "worker:w1" / "mentor"
    payload: dict[str, Any] = Field(default_factory=dict)
    idem_key: str                    # 重放去重


# ─────────────────────────── mission ───────────────────────────

class AcceptanceItem(BaseModel):
    """验收项必须绑定对象（P1-2）：target 必填，path 可选。

    匹配是三要素 (evidence, target, path)：线索的 kind 命中 evidence，
    且线索绑定的对象（主机 / 路径）与验收项一致——禁止只比 kind。
    无对象证据类型（shell.ok / shell.error / fs.result）不得作验收。"""
    id: str
    item: str
    evidence: str
    target: str
    path: str | None = None
    judged_by: Literal["auto", "jev", "human"] = "auto"


class PlanSketchItem(BaseModel):
    """规划草案（do 立面）：LLM 在理解意图/维度后给出的任务分解设想，
    展示给用户确认，并作为 planner 简报的引导（非强制——运行期计划仍由
    planner 依据执行证据修订）。goal 必填；kind/why 可空。"""
    goal: str
    kind: str = ""
    why: str = ""


class MissionSpec(BaseModel):
    objective: str
    targets: list[str]
    acceptance: list[AcceptanceItem]
    env_class: Literal["lab", "staging", "production"] = "lab"
    budget: Budget
    deadline: datetime | None = None
    allowed_scope: list[str]         # CIDR:ports / host:ports
    max_workers: int = 4
    # 意图理解（do 立面）：LLM 起草的意图摘要与评估维度，进 planner 简报
    # 引导规划；空值 = 未起草（旧 mission.json 兼容，默认空）
    intent: str = ""
    dimensions: list[str] = Field(default_factory=list)
    # 规划草案：同一次起草调用产出的任务分解设想（展示 + 简报引导）
    plan_sketch: list[PlanSketchItem] = Field(default_factory=list)


class MissionState(BaseModel):
    """由账本投影出来的内存状态；可随时重建。"""
    spec: MissionSpec
    mission_id: str
    tasks: dict[str, Task] = Field(default_factory=dict)
    # 任务 id 单调计数器（裁定 4）：t-%04d 的序号源，随 task.created 事件
    # 落盘，重放保持一致——不用 len(tasks)+1，避免归档/乱序撞号
    task_seq: int = 0
    findings: list[FindingProposal] = Field(default_factory=list)
    adjudications: dict[str, Adjudication] = Field(default_factory=dict)
    clues: list[Clue] = Field(default_factory=list)
    # 线索单调计数器：clue.added 重放时递增（序号 = 账本序，快照续接），
    # 供 context 渲染（末 60 窗口）与 briefing 的 clue_cursor
    clue_seq: int = 0
    current_strategy: dict[str, Any] = Field(default_factory=dict)
    spent_tokens: int = 0
    spent_seconds: int = 0
    step: int = 0
    finished: bool = False
    finish_summary: str = ""
    finish_reason: str = ""           # completed | budget_steps | budget_tokens
                                      # | plateau | deadline | interrupted
    # 行为统计缓存（裁定 2）：no_progress_steps / plan_rewrites / restarts_same_task
    # / virtual_success_count / planned，由 store.derive_stats 从事件账本重算，
    # 不允许存在只在内存、重启即丢的行为参数。
    meta: dict[str, Any] = Field(default_factory=dict)
