"""主循环骨架（WF1 进场 → WF2 一步循环；"一步 = 主 LLM 一次循环"）。

M1 边界（诚实声明，不做假实现）：
- 一步序 = 打注意力包 → main_llm.step → 逐个 dispatch → WF8 收尾事务
  （steps + 小票 + 水位线 mark_step 同一笔提交）→ 提交后补发 ticket_appended
  → audit → 失速计收数。
- 异常分支（W1-6）：步数线 try_spend（200 默认，撞限 E_BUDGET_EXHAUSTED 停执行）
  → 失速计 N=5（连续工具失败 E_STALL）→ LLM 侧 E_SCHEMA/E_TIMEOUT_LLM（schema
  失败落 voided step 原样上抛）——撞限落 stalled、其余 SecptError 落 abnormal、
  done=True 落 normal，统一 end_task + end_run 后收束（LlmError 仍上抛给 main.py
  兜底，W1-7 转半场报告；错误矩阵语义一致，不吞错）。
- 断电续跑（WF8）：run() 进场跑 fresh；resume(task_id) 经 checkpointer 复建——
  只复建不回放，step_no 从水位线+1 续，run id 跨 kill 连续。
- M2（W3-1）：经验回放进包（store.experience.fetch_replay 同环境 top-k）；
  收尾蒸馏 hook（WF10）挂在收束段后（先账后蒸），learning 是可降级器官——
  蒸馏失败只告警降级，绝不改变收束结论与已落账的 exit_kind。
- 事件：run_step_started / ticket_appended（事务提交后补发，绝不 pre-commit 发
  未落账的账）/ run_ended 由本循环/store 触发；exit_passed|failed 属 W2-1。
- 三问投影从账本还原（target=tasks.goal、objective/success_criteria=runs.config_json）：
  fresh 与 resume 走同一条还原路径，不依赖内存状态。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

from secpt.core import budget, checkpointer, fsm, stall
from secpt.errors import LlmError, SecptError
from secpt.llms.client import Goal, StepOutput
from secpt.llms.coach import Coach
from secpt.llms.main_llm import MainLlm
from secpt.ob.events import EventBus
from secpt.store import attention, experience, graph, ledger
from secpt.store.experience import ExperienceRow
from secpt.store.ledger import FindingRow, RunRow, TaskRow
from secpt.tools.registry import ToolOutput

_log = structlog.get_logger(__name__)

# 异常出口矩阵（§9 外圈）：这些 code 是"跑不动/没进展"，算 stalled（撞限类）；
# 其余（E_SCHEMA/E_TIMEOUT_LLM/E_LEDGER…）算 abnormal（事故类），都落 tasks.exit_kind
# 后收束，绝不让循环吞错悬死。
_STALLED_CODES: frozenset[str] = frozenset({"E_BUDGET_EXHAUSTED", "E_STALL", "E_WALL"})

# 出场三验连败上限（WF6）：每次 done 都先过验，不过弹回继续走步，3 轮仍不过直接
# 走异常出口——否则会陷入"LLM 反复说 done、反复被挡"的纠缠循环。
_EXIT_CHECK_MAX_STREAK = 3


class ExitVerdict(BaseModel):
    """出场三验的裁决形状：core 只约定形状，具体核对规则由 finalize.exit_checklist 提供。"""

    passed: bool
    failures: list[str] = Field(default_factory=list)


class ExitChecker(Protocol):
    """出场三验口（依赖倒置）：core 不 import finalize（分层红线单向），
    装配方把 ExitChecklist 实例从最外层塞进来。"""

    def check(self, conn: sqlite3.Connection, task_id: str) -> ExitVerdict:
        """按 WF6 三条核对账本：账本交齐 / 无 claimed 悬置 / 进场三问齐全。"""
        ...


class Dispatcher(Protocol):
    """工具分派口：tools/registry.ToolRegistry 结构性实现本协议（core→tools 单向）。"""

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        """schema 校验 + 执行 + 回执；失败/未知工具也回 ToolOutput（exit_code≠0/null 落小票）。"""
        ...


class Distiller(Protocol):
    """经验蒸馏口（依赖倒置）：core 不 import finalize（分层红线单向），
    装配方把 finalize.experience.Distiller 实例从最外层塞进来。"""

    def distill(
        self, conn: sqlite3.Connection, task_id: str, environment: str
    ) -> list[ExperienceRow]:
        """收尾蒸馏：候选 = 账本全 findings + 成功小票 → LLM 写摘要 → store 收口落账。"""
        ...


JudgeSweep = Callable[[sqlite3.Connection, str], list[FindingRow]]
"""裁判收口口（依赖倒置，core 不 import judge 包，分层红线单向）。

装配方 bind `functools.partial(judge.sweep, profile=prof)`——core 只见
(conn, task_id) → 被裁决行 的 callable；judged 审计在 sweep 内部落
（actor=judge）。None = 裁判不在装配（M1 兼容）。
"""

# M2 注意力包的规划段占位文案：救场提案（WF5）会临时替换它一个步，用过即还原。
_BASE_PLAN_NOTE = "（M1 主循环暂无规划重排，A/B/C 规划接线在 M2）"


class StepOutcome(BaseModel):
    """一步的落账快照：喂 W1-7 的 CLI 摘要行与 W2-3 的 e2e 断言。"""

    step_no: int
    step_id: str
    output: StepOutput
    ticket_count: int


class LoopResult(BaseModel):
    """整个 run 的结果：task/run 身份 + 步数 + 收束类型（§9：normal/abnormal/stalled）。"""

    task_id: str
    run_id: str
    steps_taken: int
    finished_naturally: bool
    exit_kind: str
    outcomes: list[StepOutcome]


class PentestLoop:
    """主循环（编排 = FSM 前身；W1-6 四态机接入后改为显式迁移序列）。

    只依赖注入件（conn/main_llm/dispatcher/bus），无全局可变状态；同一实例不缓存
    任何一步的上下文——黑板（账本）才是唯一真相。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        main_llm: MainLlm,
        dispatcher: Dispatcher,
        bus: EventBus | None = None,
        *,
        exit_checker: ExitChecker,
        distiller: Distiller | None = None,
        experience_env: str = "lab",
        judge_sweep: JudgeSweep | None = None,
        judge_note: str | None = None,
        coach: Coach | None = None,
    ) -> None:
        self._conn = conn
        self._llm = main_llm
        self._dispatcher = dispatcher
        self._bus = bus
        self._exit_checker = exit_checker
        self._distiller = distiller
        self._experience_env = experience_env
        self._judge_sweep = judge_sweep
        self._judge_note = judge_note
        self._coach = coach

    def enter(self, user_input: str) -> tuple[TaskRow, RunRow]:
        """WF1 进场：LLM 固化三问 → tasks + runs 两行落账。空 goal 视为对话模式拒入。"""
        goal_text = user_input.strip()
        if not goal_text:
            raise ValueError("空 goal：对话模式由 CLI 直接应答，不进闭环")
        goal = self._llm.entrance(goal_text)
        task = ledger.start_task(self._conn, input_text=goal_text, goal=goal.target)
        run = ledger.start_run(
            self._conn,
            task.id,
            config={
                "objective": goal.objective,
                "success_criteria": goal.success_criteria,
            },
        )
        self._audit(task.id, "进场三问完成")
        if self._judge_note:
            # W3-2：裁判装配状态在进场时烙 audit（开启/关闭/降级三态一句话）——
            # 裁判是否在岗是 run 级事实，随时可从账本复算。
            self._audit(task.id, f"裁判装配：{self._judge_note}")
        return task, run

    def run(self, user_input: str, *, max_steps: int = 200) -> LoopResult:
        """进场 + 主循环直到 done 或步线耗尽。中线预算/失速/出场纪律由后续 W 卡插入。"""
        task, run = self.enter(user_input)
        return self._advance(task, run, start=1, max_steps=max_steps)

    def resume(self, task_id: str, *, max_steps: int = 200) -> LoopResult:
        """断电续跑（WF8）：复建进场包 → 从水位线+1 续走，复用原 run id。

        不再走进场三问——目标投影从账本还原（checkpointer.resume_load）；
        已正常收尾的 run 拒续（LedgerError 上抛）。
        """
        state = checkpointer.resume_load(self._conn, task_id)
        self._audit(task_id, f"resume: run 复用 {state.run.id}, 水位线 {state.waterline}")
        return self._advance(state.task, state.run, start=state.waterline + 1, max_steps=max_steps)

    def _advance(self, task: TaskRow, run: RunRow, *, start: int, max_steps: int) -> LoopResult:
        """共用循环体：fresh 从 step 1 起跑、resume 从水位线+1 起跑（不重放已落账步）。

        收束三分支统一落账（WF7）：done → exit_kind=normal；步线走完没 done /
        撞限 → stalled（E_BUDGET_EXHAUSTED）；工序异常（schema/超时/失速）→ 按
        异常出口矩阵落 abnormal/stalled 并把 SecptError 原样上抛——本方法
        永远保证 end_task + end_run 先落账，绝不出现"炸了但账没收"。
        """
        machine = fsm.Fsm()
        machine.go(fsm.FSMState.WORK)
        counter = stall.StallCounter()
        outcomes: list[StepOutcome] = []
        exit_streak = 0  # 出场三验连败计（W2-1）：每次 done 过验，不过弹回
        rescue_used = False  # 救场名额（WF5）：每 run 只有一次 LLMC 救场机会
        plan_note = _BASE_PLAN_NOTE  # 注意力包规划段；救场提案临时替换一个步
        exit_kind = "stalled"  # 默认 = 步线走完没 done：E_BUDGET_EXHAUSTED 撞限
        crashed: SecptError | None = None
        try:
            for step_no in range(start, max_steps + 1):
                if not budget.try_spend(max_steps, step_no):
                    break  # 撞步线：停执行（§39：非降级收尾，走异常出口落账）
                outcome, done, ring = self._one_step(task, run, step_no, counter, plan_note)
                outcomes.append(outcome)
                plan_note = _BASE_PLAN_NOTE  # 提案吸收一次即用掉：下一步还原占位
                if ring:
                    hint = self._on_stall_ring(
                        task, run, outcome.step_no, counter, plan_note, rescue_used
                    )
                    if hint is None:
                        raise SecptError(
                            "失速计拉铃：连续 N 步工具失败，无实质进展",
                            code="E_STALL",
                            context={"run_id": run.id, "step_no": outcome.step_no},
                        )
                    rescue_used = True
                    plan_note = hint
                # WF6 出场三验：done 先过验再放行——passed 正常收束；not passed
                # 弹回继续走步（emit exit_failed），连败 3 轮按 E_WALL 走异常出口。
                if not done:
                    continue
                verdict = self._exit_checker.check(self._conn, task.id)
                if verdict.passed:
                    exit_kind = "normal"
                    if self._bus is not None:
                        self._bus.emit("exit_passed", task_id=task.id, run_id=run.id, failing=0)
                    break
                exit_streak += 1
                if self._bus is not None:
                    self._bus.emit(
                        "exit_failed",
                        task_id=task.id,
                        run_id=run.id,
                        streak=exit_streak,
                        failures=verdict.failures,
                    )
                self._audit(
                    task.id, f"出场三验第 {exit_streak} 轮不过：{'；'.join(verdict.failures)}"
                )
                if exit_streak >= _EXIT_CHECK_MAX_STREAK:
                    raise SecptError(
                        "出场三验连败 3 轮，走异常出口",
                        code="E_WALL",
                        context={"task_id": task.id, "failures": verdict.failures},
                    )
        except SecptError as exc:
            exit_kind = "stalled" if exc.code in _STALLED_CODES else "abnormal"
            crashed = exc
        # 收束（WF7）：done / 步线耗尽 / 工序异常 三条来路统一走到这里——先账后出。
        ledger.end_run(self._conn, run.id)
        ledger.end_task(self._conn, task.id, exit_kind)
        self._distill(task.id)
        if crashed is not None:
            self._audit(task.id, f"异常出口 {crashed.code}: {crashed}")
            machine.go(fsm.FSMState.ABNORMAL)
        elif exit_kind == "stalled":
            self._audit(task.id, "异常出口 E_BUDGET_EXHAUSTED: 步数线走完未收工")
            machine.go(fsm.FSMState.ABNORMAL)
        else:
            machine.go(fsm.FSMState.EXIT)
        if self._bus is not None:
            self._bus.emit(
                "run_ended",
                task_id=task.id,
                run_id=run.id,
                steps=len(outcomes),
                finished=exit_kind == "normal",
                exit_kind=exit_kind,
            )
        if crashed is not None:
            raise crashed
        return LoopResult(
            task_id=task.id,
            run_id=run.id,
            steps_taken=len(outcomes),
            finished_naturally=exit_kind == "normal",
            exit_kind=exit_kind,
            outcomes=outcomes,
        )

    def _one_step(
        self,
        task: TaskRow,
        run: RunRow,
        step_no: int,
        counter: stall.StallCounter,
        plan_note: str,
    ) -> tuple[StepOutcome, bool, bool]:
        """WF2 单步：打包 → step → dispatch（批外）→ 收尾事务落账 → 失速计 → 判 done。

        为什么 dispatch 在事务边界外收集回执：工具是子进程级慢操作，不能
        占着 BEGIN IMMEDIATE 的写锁等待执行；回执收齐后一笔事务写完 steps+小票+
        水位线，提交成功才补发 ticket_appended（pre-commit 发事件 = 承认未落账的账）。
        失速计收数同样在账后；拉铃只置 ring 位返回——救场（WF5）在 _advance 的
        拉铃段裁决，本方法不再自带 E_STALL 出口。
        """
        if self._bus is not None:
            self._bus.emit("run_step_started", task_id=task.id, run_id=run.id, step_no=step_no)
        pkg = attention.build(
            goal=self._goal_projection(task, run),
            step_no=step_no,
            plan_summary=plan_note,
            recent=ledger.recent_tickets(self._conn, task.id),
            graph_summary=graph.graph_summary(self._conn, task.id),
            experiences=experience.fetch_replay(self._conn, self._experience_env),
        )
        try:
            output = self._llm.step(pkg)
        except LlmError as exc:
            if exc.code == "E_SCHEMA":
                # §9 错误矩阵：schema 反复不合 → 该步 voided 记账，原文升异常。
                ledger.record_step(
                    self._conn,
                    task.id,
                    run.id,
                    step_no,
                    json.dumps(exc.to_dict(), ensure_ascii=False),
                    status="voided",
                )
            raise
        receipts = [
            self._dispatcher.dispatch(call.name, {k: str(v) for k, v in call.args.items()})
            for call in output.tool_calls
        ]
        # M3-1/M3-2：Guard 拒绝（denied）与 HITL 批准（hitl_approved）都由主循环
        # 落 audit actor=main——registry ledger-free，此处是唯一握有 task_id 与
        # 审批事实的地方；批/拒两路各有账（完成定义②）。
        for receipt in receipts:
            if receipt.denied:
                ledger.audit(self._conn, task.id, "main", receipt.summary)
            elif receipt.hitl_approved:
                ledger.audit(
                    self._conn,
                    task.id,
                    "main",
                    f"HITL 批准（{receipt.hitl_note}）: {receipt.tool_name}",
                )
        with checkpointer.step_transaction(self._conn):
            step = ledger.record_step(
                self._conn, task.id, run.id, step_no, output.model_dump_json(exclude_none=True)
            )
            tickets = [
                ledger.append_ticket(
                    self._conn,
                    task.id,
                    run.id,
                    step.id,
                    r.tool_name,
                    r.args,
                    r.exit_code,
                    r.summary,
                    r.raw_ref,
                    bus=None,  # 批内绝不发事件：事件必须发生在提交之后
                )
                for r in receipts
            ]
            # W3-2 声明落账（拍板①）：declare_finding 的受理小票在该事务内原子
            # 转 finding——回执在账也在，小票丢了账不会独活；写走 ledger 官门，
            # 本循环保持"黑板写入口只经 store/*"的纪律。真伪裁决不在本事务：
            # 先提交，再交 sweep 刻点逐条裁决（先账后判）。
            for receipt in receipts:
                if (
                    receipt.tool_name == "declare_finding"
                    and receipt.exit_code == 0
                    and receipt.raw_ref
                ):
                    ledger.append_finding(
                        self._conn,
                        task.id,
                        receipt.args.get("title", ""),
                        receipt.args.get("detail", ""),
                        receipt.raw_ref,
                    )
            checkpointer.mark_step(self._conn, run.id, step_no)
        for ticket in tickets:
            if self._bus is not None:
                self._bus.emit("ticket_appended", **ledger.ticket_event_payload(ticket))
        # W3-2 判零刻点（WF4）：事务已提交、新 claim 在账，立即无条件裁决一遍。
        self._judge_pass(task.id)
        # 失速计收数（E_TOOL_FAIL 每张异常回执 +1，成功回执清零连败；数满 N 置铃位）。
        ring = False
        for receipt in receipts:
            if counter.observe(ok=receipt.exit_code == 0):
                ring = True
                break  # 同 M1 语义：铃响本轮就算，余下回执不再收数
        self._audit(task.id, f"step {step_no}: {output.thought}, tool_calls={len(tickets)}")
        outcome = StepOutcome(
            step_no=step_no, step_id=step.id, output=output, ticket_count=len(tickets)
        )
        return outcome, output.done, ring

    def _on_stall_ring(
        self,
        task: TaskRow,
        run: RunRow,
        step_no: int,
        counter: stall.StallCounter,
        plan_note: str,
        rescue_used: bool,
    ) -> str | None:
        """WF5 失速救场段：事件线落铃 → 教练提案；返回下一步的规划段（hint）。

        顺序契约（先账后铃）：本段在 step_transaction 提交之后才被调，铃必落在
        已落账的事实上。三个分支语义——
        - 名额没救（coach 缺位 / 已用掉）：返回 None，由调用方按 M1 原语义 raise
          E_STALL（事件线只管报告，绝不自动接管——R3 风险红线）；
        - 教练失能（LlmError/idea 空白）：可降级器官——warning + audit(actor=main)
          留痕后同样返回 None，绝不悬死也不遮错；
        - 提案在手：audit(actor=llmc) 落账 → reset 连败 → hint 挤进下一步注意力包。
        """
        if self._bus is not None:
            self._bus.emit("stall_alarm", task_id=task.id, run_id=run.id, step_no=step_no)
        if self._coach is None or rescue_used:
            return None
        try:
            proposal = self._coach.propose(graph.graph_summary(self._conn, task.id), plan_note)
        except Exception as exc:  # noqa: BLE001  可降级器官兜底（见 docstring）
            _log.warning("coach_degraded", task_id=task.id, error=str(exc))
            self._audit(task.id, f"教练提案降级：{exc}")
            return None
        if proposal is None:
            _log.warning("coach_no_proposal", task_id=task.id)
            self._audit(task.id, "教练提案降级：LLM 无话说，不救场（只提案不裁决红线不变）")
            return None
        hint = f"教练提案待吸收（WF5 救场，只提案不裁决）：{proposal.idea}——{proposal.reason}"
        ledger.audit(self._conn, task.id, "llmc", f"教练提案（只提案不裁决）：{hint}")
        counter.reset()
        _log.info("coach_proposal_accepted", task_id=task.id)
        return hint

    def _judge_pass(self, task_id: str) -> None:
        """W3-2 判零刻点（WF4）：每步收尾提交后立即裁决本轮新 claim。

        分层红线：core 不 import judge——sweep 由装配方以 partial 注入（JudgeSweep
        类型别名），core 只见 callable。刻点幂等：sweep 只取 verify_state="claimed"
        行，终态行永不重复进裁决；关闭（judge_sweep=None）时无事发生。
        汇总一行 audit(actor=main) 佐证"裁判收口"，每条裁决明细由 sweep 内部
        落 audit(actor=judge)。LedgerError（终态重判等）不在此拦——那是契约
        破坏，原样上抛走异常出口。
        """
        if self._judge_sweep is None:
            return
        verdicts = self._judge_sweep(self._conn, task_id)
        if verdicts:
            self._audit(task_id, f"裁判收口 {len(verdicts)} 条 claim")
            _log.info("judge_swept", task_id=task_id, verdicts=len(verdicts))

    def _distill(self, task_id: str) -> None:
        """收尾经验蒸馏 hook（WF10）：任何出口（normal/stalled/abnormal）都收尾进。

        为什么吞在这里：learning 是可降级器官——蒸馏失败（LlmError/账本故障）绝不能
        改变收束结论（end_task 已落账）或掩盖原异常（crashed 也可能在此之后 re-raise），
        但降级必须有痕：structlog 告警（机器频道）+ audit 一行（账本证据）。
        Distiller 对非法 verify_state 抛的 LedgerError 不在此拦——那是代码缺陷，
        不是降级场景，静默吞掉会掩盖 bug。
        """
        if self._distiller is None:
            return  # M1 老装配（无蒸馏器）：hook 不存在，无事发生
        try:
            rows = self._distiller.distill(self._conn, task_id, self._experience_env)
        except Exception as exc:  # noqa: BLE001  可降级器官兜底（设计边界，见 docstring）
            _log.warning("experience_distill_degraded", task_id=task_id, error=str(exc))
            ledger.audit(self._conn, task_id, "main", f"经验蒸馏降级：{exc}")
            return
        _log.info(
            "experience_distilled",
            task_id=task_id,
            environment=self._experience_env,
            entries=len(rows),
        )

    def _goal_projection(self, task: TaskRow, run: RunRow) -> Goal:
        """从账本还原进场三问（不依赖任何内存态，resume 路径同一个函数）。"""
        cfg = json.loads(run.config_json)
        return Goal(
            target=task.goal or "",
            objective=str(cfg.get("objective", "")),
            success_criteria=str(cfg.get("success_criteria", "")),
        )

    def _audit(self, task_id: str, what: str) -> None:
        """审计 actor=main（§3.6 对表：主循环落账一律 main）。"""
        ledger.audit(self._conn, task_id, "main", what)


__all__ = ["Dispatcher", "JudgeSweep", "LoopResult", "PentestLoop", "StepOutcome", "ToolOutput"]
