"""首个计划与每轮编排（BUILD_SPEC §7.3；编排权迁移裁定）。

bootstrap_plan 只在 mission 引导时调用一次；orchestrate 是每轮的
decide 回合：模型给一轮 RoundDirective（run 既有 ready 任务 + create
新任务草案），过确定性护栏后才落账执行。编排权在智能体侧，确定性地基
留在引擎侧——两者的边界是护栏：

- run：只认存在且 ready 的任务 id，批上限 = max_workers，重复剔除；
- create：每轮 ≤ CREATE_CAP，coach 结构检查，创建后 DAG 校验；
- 模型调用/解析失败不炸引擎：静默降级 fallback_directive（缩编前
  的确定性批调度语义），失败与空指令都记录在 decision.made 的
  mode 字段里——可观测，不吞错误。

三种降级来源（mode 前缀 fallback:*）区分：ModelCallError / ModelParseError
/ empty（run 与 create 双空的死锁防线）。幂等：context.assembled /
decision.made / task.created 的 idem key 都带轮次号（step+1，结算前
step 未增、重放后确定性还原），崩溃重入同一轮不会产生第二份计划。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from pa.core import experience
from pa.core.context import assemble, section_sizes
from pa.errors import ModelCallError, ModelParseError
from pa.graph.ready import ready_tasks, validate_dag
from pa.graph.task import create_task
from pa.infra.llm import complete_retry
from pa.models import Criterion, MissionState, Task, TaskDraft
from pa.policy import coach, review
from pa.policy.thresholds import THRESHOLDS
from pa.store.writer import StoreWriter

CREATE_CAP = 6  # 单轮 create 上限：防无穷造任务吃预算


class PlanDraft(BaseModel):
    tasks: list[TaskDraft]
    current_strategy: dict = Field(default_factory=dict)
    rationale: str = ""


class RoundDirective(BaseModel):
    """单轮编排指令。

    run 只允许引用本轮仍处于 ready 的任务 id——越界引用会被 _guard_run
    剔除而不是报错；create 每轮至多 CREATE_CAP 个。current_strategy
    非空时随 decision.made 的语义更新（旧值保留，除非模型明确给新值）。

    abandon：编排者主动废止的 task id（连续失败/假设已死/被替代）。这是
    编排者对引擎自动重排队的唯一否决权——没有它，rationale 里写“不再
    重派”只是空话，_requeue_failed 下一轮仍会把任务抬回 ready。
    """

    run: list[str] = Field(default_factory=list)
    create: list[TaskDraft] = Field(default_factory=list)
    abandon: list[str] = Field(default_factory=list)
    current_strategy: dict = Field(default_factory=dict)
    rationale: str = ""


def _split_target(target: str) -> tuple[str, int | None]:
    """target → (host, port)；URL/CIDR 只取主机，裸主机不给端口。"""
    from urllib.parse import urlsplit

    t = str(target or "").strip()
    if t.startswith(("http://", "https://")):
        u = urlsplit(t)
        return (u.hostname or t), (u.port or (443 if u.scheme == "https" else 80))
    if "/" in t:
        return t.split("/", 1)[0], None
    if ":" in t and t.rsplit(":", 1)[-1].isdigit():
        host, _, port = t.rpartition(":")
        return host, int(port)
    return t, None


def _fallback_plan(state: MissionState) -> PlanDraft:
    """planner 失败/空计划时的确定性基线：端口、HTTP 根路径、TLS。

    顶级 agent 不能在唯一 planner 调用失败时 0 任务躺平。这里只使用
    已登记的证据类型（probe.open / http.status.200 / tls.cert）和已存在
    的工具，保证 fallback 任务本身可执行、可判定。
    """
    tasks: list[TaskDraft] = []
    seen: set[str] = set()

    def add(
        typ: str,
        goal: str,
        evidence: str,
        tools: list[str],
        host: str,
        port: int | None,
        role: str,
    ) -> None:
        if goal in seen or len(tasks) >= 8:
            return
        seen.add(goal)
        tasks.append(
            TaskDraft(
                type=typ,
                goal=goal,
                success_criteria=[Criterion(evidence=evidence)],
                scope_slice={"host": host, "port": port},
                allowed_tools=tools,
                role=role,
            )
        )

    for target in state.spec.targets:
        host, port = _split_target(target)
        if not host:
            continue
        ports = [port] if port is not None else [80, 443]
        for p in ports:
            add(
                "probe",
                f"探测 {host}:{p} 的端口状态",
                "probe.open",
                ["probe"],
                host,
                p,
                "recon",
            )
            if p == 443:
                add(
                    "tls",
                    f"采集 {host}:{p} 的 TLS 证书与协议配置",
                    "tls.cert",
                    ["tls"],
                    host,
                    p,
                    "recon",
                )
            if p in (80, 443):
                scheme = "https" if p == 443 else "http"
                add(
                    "http",
                    f"请求 {scheme}://{host}:{p}/ 根路径并记录响应",
                    "http.status.200",
                    ["http"],
                    host,
                    p,
                    "web-app",
                )
    rationale = (
        "planner 不可用或未产出可执行任务，使用确定性基线计划："
        "端口探活 → HTTP 根路径 → TLS 采集"
    )
    return PlanDraft(
        tasks=tasks,
        current_strategy={"approach": "deterministic-baseline"},
        rationale=rationale,
    )


def bootstrap_plan(state: MissionState, writer: StoreWriter, llm, assets) -> None:
    """引导首个计划：planner 决策 → coach 结构检查 → create_task → plan.created。
    current_strategy 随事件落盘（apply_event 重放恢复）。

    planner phase 也发 context.assembled（S13 裁定 3）——同样只记尺寸。

    E3：新 mission 启动时注入长期经验全库（payload["lessons"]，L165
    「新 mission 启动时注入」的落点）；空库不产出该段，旧行为逐字节
    不变。只读——写路径只有收口时的 distill。"""
    payload = {"lessons": experience.all_lessons(writer.root)}
    system, user = assemble("planner", state, payload, assets)
    writer.append(
        "context.assembled",
        "planner",
        {
            "task_id": "-",
            "attempt": 0,
            "step": 0,
            "phase": "planner",
            **section_sizes("planner", state, payload, assets),
        },
        f"ctx:plan:{state.mission_id}:1",
    )
    mode = "planner"
    try:
        raw = complete_retry(
            llm, retries=1, system=system, user=user, schema=PlanDraft, phase="planner"
        )
        plan = llm.parse(raw, PlanDraft)
    except (ModelCallError, ModelParseError) as e:
        mode = "fallback:planner_error"
        writer.append(
            "decision.made",
            "core",
            {
                "kind": "plan_error",
                "mode": mode,
                "error_type": type(e).__name__,
                "detail": str(e)[:500],
            },
            f"dm:plan:{state.mission_id}:error",
        )
        plan = _fallback_plan(state)
    # 合同门：散文判据 / fs.read / shell.output 这类不可判定证据在
    # criteria_met 里永远匹配不到，必须在创建前拒绝；全部拒绝时回落
    # 确定性基线计划，而不是让 mission 0 任务躺平。
    valid, rejected = review.filter_valid_drafts(plan.tasks)
    for d, bad in rejected:
        writer.append(
            "mentor.intervened",
            "mentor",
            {
                "level": "I4",
                "issue": "bad_criterion",
                "detail": {"goal": d.goal[:120], "criteria": bad},
            },
            f"plan:bad:{state.mission_id}:{d.goal[:40]}",
        )
    if not valid:
        if plan.tasks:
            mode = "fallback:invalid_criteria"
            writer.append(
                "decision.made",
                "core",
                {
                    "kind": "plan_error",
                    "mode": mode,
                    "detail": "planner 产出的判据全部不可判定，回落基线计划",
                },
                f"dm:plan:{state.mission_id}:invalid",
            )
        plan = _fallback_plan(state)
        valid = plan.tasks
    plan.tasks = valid
    # P0-2：planner 的依赖编排一律不采用——v0 任务全部并列，可解析的
    # deps 直接置空（保留 deps 只会制造「依赖未完成 → 永不派发」的静默
    # 死锁）。悬空 deps（指向不存在的任务）保留下来交给 validate_dag
    # 诊断：置 blocked(dangling_dep) + mentor.intervened(I3)，绝不留在
    # pending 里空等。
    known = set(state.tasks) | review.batch_ids(state.task_seq, len(plan.tasks))
    for d in plan.tasks:
        if all(x in known for x in d.deps):
            d.deps = []
    for d in coach.on_plan(state, writer, plan.tasks):
        create_task(state, writer, d)
    # 兜底：create_task 后立即校验任务图（validate_dag 幂等，重放安全）
    validate_dag(state, writer)
    state.current_strategy = plan.current_strategy
    writer.append(
        "plan.created",
        "core",
        {
            "n": len(plan.tasks),
            "strategy": plan.current_strategy,
            "rationale": plan.rationale,
            "mode": mode,
        },
        f"plan:{state.mission_id}:1",
    )


def fallback_directive(state: MissionState) -> RoundDirective:
    """确定性兜底（护栏的最后一道）：模型不可用 / 给了空指令时，按
    优先级取 ready 批——与缩编前的调度语义逐字节一致，保证「编排
    智能体缺席时引擎不瘫痪」（decide 是增强，不是依赖）。"""
    batch = [t.id for t in ready_tasks(state) if t.state == "ready"]
    return RoundDirective(
        run=batch[: max(1, state.spec.max_workers)],
        rationale="确定性兜底：按优先级取 ready 批",
    )


def _recent_advisories(writer: StoreWriter, limit: int = 5) -> list[dict]:
    """最近教练建议（只读账本；orchestrator 必须看到，否则 shadow 等于没教练）。"""
    from pa.store.view import StoreView

    out: list[dict] = []
    for ev in reversed(list(StoreView(writer.root).events())):
        if ev.type not in ("mentor.advisory", "mentor.advisory_agg"):
            continue
        out.append(
            {
                "type": ev.type,
                "level": ev.payload.get("level"),
                "issue": ev.payload.get("issue"),
                "target": ev.payload.get("target"),
                "metric": ev.payload.get("metric"),
                "would_do": ev.payload.get("would_do"),
                "delta": ev.payload.get("delta"),
            }
        )
        if len(out) >= limit:
            break
    return list(reversed(out))


def orchestrate(state: MissionState, writer: StoreWriter, llm, assets) -> list[Task]:
    """一轮 decide：context → LLM RoundDirective → 确定性护栏 → 落账。

    返回本轮实际执行的 Task 列表（护栏过滤后，调用方逐个派发）。
    每轮发两种账面事件：context.assembled（只记各段尺寸，不记目标
    正文）与 decision.made(kind=orchestrate)（run/created/mode/
    rationale——编排决策的完整审计入口）。"""
    n = state.step + 1  # 本轮序号：结算前 step 未增，重放确定
    ready = [t for t in ready_tasks(state) if t.state == "ready"]
    payload = {
        "step": n,
        "ready": [
            {
                "id": t.id,
                "type": t.type,
                "goal": t.goal,
                "priority": t.priority,
                "attempt": t.attempt,
                "role": t.role,
            }
            for t in ready
        ],
        # E3：每轮给编排者长期经验概览（全库；worker 侧才做类型精确
        # 过滤——编排者是跨类型的分派决策方）。空库不产出该段。
        "lessons": experience.all_lessons(writer.root),
        # 教练建议不能只躺在账本里：orchestrator 是唯一编排点，必须看到
        # 最近 I2/I1 信号，否则 shadow 模式等于没有教练。
        "advisories": _recent_advisories(writer),
    }
    system, user = assemble("orchestrator", state, payload, assets)
    writer.append(
        "context.assembled",
        "core",
        {
            "task_id": "-",
            "attempt": 0,
            "step": n,
            "phase": "orchestrator",
            **section_sizes("orchestrator", state, payload, assets),
        },
        f"ctx:orch:{state.mission_id}:{n}",
    )
    directive, mode = _decide(state, llm, system, user)
    # abandon 先于 run/create：同一 id 同时出现在 run/abandon 时，废止
    # 优先，避免“刚说要放弃又派发”的自相矛盾。
    abandoned = _guard_abandon(state, writer, directive.abandon)
    run_tasks = _guard_run(state, directive.run)
    created, create_dropped = _guard_create(state, writer, directive.create)
    if not run_tasks and not created:
        # 死锁防线：指令既不执行也不创建 → 空轮永动。降级兜底批。
        directive, mode = fallback_directive(state), "fallback:empty"
        run_tasks = _guard_run(state, directive.run)
    if directive.current_strategy:
        state.current_strategy = dict(directive.current_strategy)
    payload = {
        "kind": "orchestrate",
        "round": n,
        "mode": mode,
        "run": [t.id for t in run_tasks],
        "created": [t.id for t in created],
        "abandoned": abandoned,
        "create_dropped": create_dropped,
        "rationale": directive.rationale[:500],
    }
    # 策略是编排者的“为什么这么排”的核心；空值不写，旧账本/旧行为不变。
    # 流程日志与 secpt log 靠它回答「这一轮为什么换方向」。
    if directive.current_strategy:
        payload["current_strategy"] = dict(directive.current_strategy)
    writer.append(
        "decision.made",
        "core",
        payload,
        f"dm:orch:{state.mission_id}:{n}",
    )
    return run_tasks


def _decide(
    state: MissionState, llm, system: str, user: str
) -> tuple[RoundDirective, str]:
    """模型 decide：调用/解析失败不上抛（可降级功能），降级路径把
    失败类型写进 mode 与 rationale——账本可查，排障不靠猜。"""
    try:
        raw = complete_retry(
            llm,
            retries=1,
            system=system,
            user=user,
            schema=RoundDirective,
            phase="orchestrator",
        )
        return llm.parse(raw, RoundDirective), "orchestrator"
    except (ModelCallError, ModelParseError) as e:
        fb = fallback_directive(state)
        fb.rationale = f"模型决策失败（{type(e).__name__}），降级确定性批调度"
        return fb, f"fallback:{type(e).__name__}"


def _guard_abandon(state: MissionState, writer: StoreWriter, ids: list[str]) -> list[str]:
    """编排者废止任务：failed/blocked/ready/pending → superseded。

    幂等：已 superseded/done 的任务跳过；同一个 id 重复引用只记一次。
    这是 _requeue_failed 的否决权——废止后的任务不会再被自动抬回 ready。
    """
    from pa.graph.state import transition

    out: list[str] = []
    seen: set[str] = set()
    for tid in ids:
        if tid in seen:
            continue
        seen.add(tid)
        t = state.tasks.get(tid)
        if t is None or t.state in ("done", "superseded"):
            continue
        if t.state not in ("pending", "ready", "failed", "blocked"):
            continue
        transition(t, "superseded", writer, reason="abandoned")
        out.append(tid)
    return out


def _guard_run(state: MissionState, ids: list[str]) -> list[Task]:
    """run 护栏：id 必须存在且处于 ready；批上限 = max_workers；
    重复引用去重。坏引用剔除而非抛错——不连累指令里的好部分。"""
    cap = max(1, state.spec.max_workers)
    out: list[Task] = []
    seen: set[str] = set()
    for tid in ids:
        if len(out) >= cap:
            break
        if tid in seen:
            continue
        t = state.tasks.get(tid)
        if t is None or t.state != "ready":
            continue
        seen.add(tid)
        out.append(t)
    return out


def _norm_goal(goal: str) -> str:
    """goal 归一化：去空白/标点、小写——「枚举常见敏感路径。」与
    「枚举常见敏感路径」是同一件事。只用于 create 去重，不改写 goal。"""
    return "".join(ch for ch in str(goal or "").lower() if ch.isalnum())


def _guard_create(
    state: MissionState, writer: StoreWriter, drafts: list[TaskDraft]
) -> tuple[list[Task], int]:
    """create 护栏：先截断到 CREATE_CAP，再过 coach 结构检查
    （enforce 档会拦掉带 I4 缺陷的草案），创建后立即 DAG 校验。

    去重两道：
    - goal 归一化去重：已存在（未废止）任务的归一化 goal 不再创建——
      「汇总结论」换措辞再来第三遍是计划膨胀的典型形态（真跑 t-0007/
      t-0011 教训）；superseded 的 goal 允许重建（替代任务场景）。
    - mission 任务上限（THRESHOLDS["mission_task_cap"]）：超过后本轮
      create 整批拒绝并记 I3 介入——预算是防失控的硬界，不是建议。
    """
    existing = {
        _norm_goal(t.goal)
        for t in state.tasks.values()
        if t.state != "superseded"
    }
    cap = int(THRESHOLDS.get("mission_task_cap", 24))
    live = sum(1 for t in state.tasks.values() if t.state != "superseded")
    room = max(0, cap - live)
    seen: set[str] = set()
    batch: list[TaskDraft] = []
    dropped_dupes = 0
    for d in list(drafts)[:CREATE_CAP]:
        key = _norm_goal(d.goal)
        if len(batch) >= room or not key or key in existing or key in seen:
            dropped_dupes += 1
            continue
        seen.add(key)
        batch.append(d)
    # 合同门独立于 coach 档位：不可判定判据在 shadow 下也不能创建，
    # 否则会进入 3 次重试的确定性失败循环（真跑 t-0007/t-0011 教训）。
    valid, rejected = review.filter_valid_drafts(batch)
    for d, bad in rejected:
        writer.append(
            "mentor.intervened",
            "mentor",
            {
                "level": "I4",
                "issue": "bad_criterion",
                "detail": {"goal": d.goal[:120], "criteria": bad},
            },
            f"create:bad:{state.mission_id}:{d.goal[:40]}",
        )
    made = [
        create_task(state, writer, d)
        for d in coach.on_plan(state, writer, valid)
    ]
    validate_dag(state, writer)
    # 去重/上限拒绝量由调用方随 decision.made 落账；不写 state.meta——
    # meta 只从账本重算（裁定 2），运行时私键会在 restore 后丢失。
    return made, dropped_dupes
