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
    # 默认空 dict：缺 scope 的草案能通过指令 schema 校验走到 coach 护栏，
    # 由 review (empty_scope) / enforce 拦截——而不是在 pydantic 边界
    # 一票否决整个 RoundDirective（一票否决会连指令里其余合格的
    # 草案一起丢掉，违反「不连累好部分」原则）。
    scope_slice: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str]
    budget: Budget = Field(default_factory=Budget)
    priority: float = 0.5
    state: TaskState = "pending"
    # 承担角色（M1）：指角色库 id（roles/*.md 的 front matter）。空 = 未
    # 指派（旧任务行为不变）。仅影响注入文本与技能过滤，没有任何门以
    # role 为硬判据——角色是增强信息，缺了不该拦截任务。
    role: str = ""
    lease: Lease | None = None
    attempt: int = 0
    # 进入 blocked 的原因（如 dangling_dep，裁定 3），随 task.state_changed
    # 落盘，重放还原
    block_reason: str | None = None
    tried: list[str] = Field(default_factory=list)
    lineage: Lineage = Field(default_factory=Lineage)


class TaskDraft(BaseModel):
    """LLM 产出的任务草案，id/mission_id 由引擎补。

    scope_slice 默认空 dict：缺 scope 的草案要能通过指令 schema 校验、
    走到 coach 护栏被 review(empty_scope) / enforce 拦截——而不是在
    pydantic 边界一票否决整个 RoundDirective（一票否决会把指令里其余
    合格草案一起丢掉，违反「不连累好部分」原则）。"""

    type: str
    goal: str
    parent: str | None = None
    deps: list[str] = Field(default_factory=list)
    preconditions: list[Precondition] = Field(default_factory=list)
    success_criteria: list[Criterion]
    abort_criteria: list[str] = Field(default_factory=list)
    scope_slice: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str]
    budget: Budget = Field(default_factory=Budget)
    priority: float = 0.5
    # 承担角色（M1）：角色库 id，空 = 未指派；随 task.created 落账，
    # 重放还原。与 scope_slice 同思路：可选注释信息不许在 pydantic 边界
    # 一票否决草案——角色写错/缺库由 context 注入时静默降级并可见。
    role: str = ""


# ─────────────────────────── 决策 ───────────────────────────


class CallTool(BaseModel):
    kind: Literal["call_tool"] = "call_tool"
    tool: str
    args: dict[str, Any]
    expect: str  # 预期观察，必填，反思的锚点
    rationale: str = ""


class CallBatch(BaseModel):
    """M2 批量执行：worker 一轮产出多个动作。

    红线：批量只是「打包产出」，不是「打包放行」——每一项都由 worker
    逐个送进网关 8 步门（存在性/范围/风险/去重/执行/证据/审计），不存在
    批量跳门。空列表 = 本轮不执行任何动作，回 ReAct 反思。"""

    kind: Literal["batch"] = "batch"
    actions: list[CallTool]
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


class FinishFinding(BaseModel):
    """worker 收口随附的发现提案（finding 管道补全）。

    claim 必须能被本任务产出的证据支撑；evidence_refs 由引擎绑定本任务
    的证据（worker 侧取本任务 evidence_refs 至多 3 条），模型不指定
    ev-id——跨任务引用/编造证据在构造时就不可能发生。"""

    claim: str
    severity: Literal["info", "low", "med", "high", "critical"] = "info"
    reproduce: str = ""
    falsify_if: list[str] = Field(default_factory=list)


class Finish(BaseModel):
    kind: Literal["finish"] = "finish"
    summary: str
    satisfied: list[str] = Field(default_factory=list)  # acceptance id
    rationale: str = ""
    # 收口随附的发现提案（引擎截断到 3 条）：「/admin/ 是 catch-all 不是
    # 暴露」「TLS 证书 50 天后到期」这类结论以前只躺在 finish summary 里，
    # 报告只能事后扫文本；现在它们成为一等 finding，进 JEV 裁决与报告。
    findings: list[FinishFinding] = Field(default_factory=list)


Decision = Annotated[
    Union[CallTool, CallBatch, Spawn, Replan, Wait, Finish],
    Field(discriminator="kind"),
]


# ─────────────────────────── 结果 ───────────────────────────

FailureClass = Literal["Env", "Tool", "Model", "Plan", "AuthZ", "Deception"]
WorkerStatus = Literal["done", "partial", "failed", "blocked", "needs_help"]


class Clue(BaseModel):
    kind: str  # host / port / service / path / cred / error
    key: str
    value: Any
    evidence_id: str | None = None
    confidence: float = 0.8
    source_task: str | None = None
    # 验收三要素之对象绑定（P1-2）：probe/http 线索必须带主机；path 仅
    # http 类线索填充（None = 不区分路径）。shell/fs 类无对象线索保持 None。
    target: str | None = None
    path: str | None = None
    # 结构化观测（HTTP headers/content-length/body hash/Location 等）。
    # 旧账本无此字段，默认空；clue.value 仍是人读摘要，meta 负责可判定细节。
    meta: dict[str, Any] = Field(default_factory=dict)


class FindingProposal(BaseModel):
    claim: str
    severity: Literal["info", "low", "med", "high", "critical"]
    evidence_refs: list[str]
    reproduce: str
    falsify_if: list[str]  # 必填：什么情况下这条不成立
    asset_id: str | None = None
    # 溯源边（WORKFLOW §8.3-4）：产出这条 finding 的 task id 列表——单次
    # 提案是单个 id，合并（derive.merge_finding）时做并集累积。老账本无此
    # 字段，默认空，回放完全兼容。
    origin: list[str] = Field(default_factory=list)


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
    "mission.created",
    "mission.finished",
    "plan.created",
    "plan.revised",
    "task.created",
    "task.state_changed",
    "task.dispatched",
    "decision.made",
    "action.started",
    "action.finished",
    "action.blocked",
    "evidence.stored",
    "clue.added",
    "finding.proposed",
    "finding.adjudicated",
    "worker.returned",
    "mentor.intervened",
    "mentor.advisory",
    "mentor.advisory_agg",
    "restart.decided",
    "budget.charged",
    "budget.warning",
    "human.directive",
    "model.switched",
    "context.assembled",
    "memory.reflect",
    "mission.audit",
]


class Event(BaseModel):
    seq: int
    mission_id: str
    ts: datetime = Field(default_factory=now)
    type: EventType
    actor: str  # "engine" / "core" / "worker:w1" / "mentor"
    payload: dict[str, Any] = Field(default_factory=dict)
    idem_key: str  # 重放去重


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


class FileRef(BaseModel):
    """mission 附件清单项（文件/目录上传的确定性登记）。

    name 是 worker 视角的 workspace 相对路径（`_attachments/<file>`，
    fs.read/grep 直接用）；origin 是用户提供的原始路径（溯源）；
    binary=True 的文件照常入 workspace 但确定性扫描跳过（无法按行
    解码）。"""

    name: str
    origin: str = ""
    size: int = 0
    sha256: str = ""
    binary: bool = False

    @property
    def basename(self) -> str:
        """验收/线索绑定用的稳定标识（_attachments/x → x）。

        stage_files 重名即跳过，basename 在 mission 内唯一——验收
        target 与 clue.target 都用它，路径前缀只留给 fs 工具。"""
        return self.name.rsplit("/", 1)[-1]


class MissionSpec(BaseModel):
    objective: str
    targets: list[str]
    acceptance: list[AcceptanceItem]
    env_class: Literal["lab", "staging", "production"] = "lab"
    budget: Budget
    deadline: datetime | None = None
    allowed_scope: list[str]  # CIDR:ports / host:ports
    max_workers: int = 4
    # 意图理解（do 立面）：LLM 起草的意图摘要与评估维度，进 planner 简报
    # 引导规划；空值 = 未起草（旧 mission.json 兼容，默认空）
    intent: str = ""
    dimensions: list[str] = Field(default_factory=list)
    # 规划草案：同一次起草调用产出的任务分解设想（展示 + 简报引导）
    plan_sketch: list[PlanSketchItem] = Field(default_factory=list)
    # 附件清单（--attachment 文件/目录）：confirm 后复制进
    # workspace/_attachments/，bootstrap 做确定性扫描（密钥/危险 sink/
    # 依赖 CVE 匹配）。旧 mission.json 无此字段，默认空完全兼容。
    files: list[FileRef] = Field(default_factory=list)


class ReflectBrief(BaseModel):
    """轮末滚动简报（E2）：LLM 提炼的单段摘要。

    max_length=2000 是冷区 digest 的第一道闸（§8.3-2）：超长输出在
    schema 校验即失败 → ModelParseError → 静默降级，不进账本。"""

    summary: str = Field(min_length=1, max_length=2000)


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
    finish_reason: str = ""  # completed | budget_steps | budget_tokens
    # | plateau | deadline | interrupted
    # L3 轮末滚动简报（E2）：最新一条 memory.reflect 的投影；rolling——
    # 新简报覆盖旧值，账本原文各轮独立可查（永不丢）。空 = 尚无简报
    # （空值不输出，orchestrator 上下文少一段，旧行为逐字节不变）。
    brief: dict[str, Any] = Field(default_factory=dict)
    # 行为统计缓存（裁定 2）：no_progress_steps / plan_rewrites / restarts_same_task
    # / virtual_success_count / planned，由 store.derive_stats 从事件账本重算，
    # 不允许存在只在内存、重启即丢的行为参数。
    meta: dict[str, Any] = Field(default_factory=dict)
