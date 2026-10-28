"""主循环骨架（WF1 进场 → WF2 一步循环；"一步 = 主 LLM 一次循环"）。

M1 边界（诚实声明，不做假实现）：
- 一步序 = 打注意力包 → main_llm.step → 逐个 dispatch → WF8 收尾事务
  （steps + 小票 + 水位线 mark_step 同一笔提交）→ 提交后补发 ticket_appended
  → audit。真正的步数线（200/≤4）/失速计/出场纪律在 W1-6/W2-1 插进本类。
- 断电续跑（WF8）：run() 进场跑 fresh；resume(task_id) 经 checkpointer 复建——
  只复建不回放，step_no 从水位线+1 续，run id 跨 kill 连续。
- 异常出口：LlmError 等分层异常本版原样上抛（main.py 兜底是 W1-7 的事），循环
  绝不吞错，也不做半路降级。
- 事件：run_step_started / ticket_appended（事务提交后补发，绝不 pre-commit 发
  未落账的账）/ run_ended 由本循环/store 触发；exit_passed|failed 属 W2-1。
- 三问投影从账本还原（target=tasks.goal、objective/success_criteria=runs.config_json）：
  fresh 与 resume 走同一条还原路径，不依赖内存状态。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel

from secpt.core import checkpointer
from secpt.llms.client import Goal, StepOutput
from secpt.llms.main_llm import MainLlm
from secpt.ob.events import EventBus
from secpt.store import attention, ledger
from secpt.store.ledger import RunRow, TaskRow
from secpt.tools.registry import ToolOutput


class Dispatcher(Protocol):
    """工具分派口：tools/registry.ToolRegistry 结构性实现本协议（core→tools 单向）。"""

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        """schema 校验 + 执行 + 回执；失败/未知工具也回 ToolOutput（exit_code≠0/null 落小票）。"""
        ...


class StepOutcome(BaseModel):
    """一步的落账快照：喂 W1-7 的 CLI 摘要行与 W2-3 的 e2e 断言。"""

    step_no: int
    step_id: str
    output: StepOutput
    ticket_count: int


class LoopResult(BaseModel):
    """整个 run 的结果：task/run 身份 + 步数 + 是否自然收工（done=True）。"""

    task_id: str
    run_id: str
    steps_taken: int
    finished_naturally: bool
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
    ) -> None:
        self._conn = conn
        self._llm = main_llm
        self._dispatcher = dispatcher
        self._bus = bus

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
        """共用循环体：fresh 从 step 1 起跑、resume 从水位线+1 起跑（不重放已落账步）。"""
        outcomes: list[StepOutcome] = []
        finished = False
        for step_no in range(start, max_steps + 1):
            outcome, done = self._one_step(task, run, step_no)
            outcomes.append(outcome)
            if done:
                finished = True
                break
        ledger.end_run(self._conn, run.id)
        if self._bus is not None:
            self._bus.emit(
                "run_ended",
                task_id=task.id,
                run_id=run.id,
                steps=len(outcomes),
                finished=finished,
            )
        return LoopResult(
            task_id=task.id,
            run_id=run.id,
            steps_taken=len(outcomes),
            finished_naturally=finished,
            outcomes=outcomes,
        )

    def _one_step(self, task: TaskRow, run: RunRow, step_no: int) -> tuple[StepOutcome, bool]:
        """WF2 单步：打包 → step → dispatch（批外）→ 收尾事务落账 → 判 done。

        为什么 dispatch 在事务边界外收集回执：工具是子进程级慢操作，不能
        占着 BEGIN IMMEDIATE 的写锁等待执行；回执收齐后一笔事务写完 steps+小票+
        水位线，提交成功才补发 ticket_appended（pre-commit 发事件 = 承认未落账的账）。
        """
        if self._bus is not None:
            self._bus.emit("run_step_started", task_id=task.id, run_id=run.id, step_no=step_no)
        pkg = attention.build(
            goal=self._goal_projection(task, run),
            step_no=step_no,
            plan_summary="（M1 主循环暂无规划重排，A/B/C 规划接线在 M2）",
            recent=ledger.recent_tickets(self._conn, task.id),
        )
        output = self._llm.step(pkg)
        receipts = [
            self._dispatcher.dispatch(call.name, {k: str(v) for k, v in call.args.items()})
            for call in output.tool_calls
        ]
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
            checkpointer.mark_step(self._conn, run.id, step_no)
        for ticket in tickets:
            if self._bus is not None:
                self._bus.emit("ticket_appended", **ledger.ticket_event_payload(ticket))
        self._audit(task.id, f"step {step_no}: {output.thought}, tool_calls={len(tickets)}")
        outcome = StepOutcome(
            step_no=step_no, step_id=step.id, output=output, ticket_count=len(tickets)
        )
        return outcome, output.done

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


__all__ = ["Dispatcher", "LoopResult", "PentestLoop", "StepOutcome", "ToolOutput"]
