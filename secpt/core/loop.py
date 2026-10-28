"""主循环骨架（WF1 进场 → WF2 一步循环；"一步 = 主 LLM 一次循环"）。

M1 边界（诚实声明，不做假实现）：
- 一步最简序 = 打注意力包 → main_llm.step → 逐个 dispatch → 落小票 + record_step
  + audit → 判 done。真正的步数线（200/≤4）/失速计/水位线同事务推进在
  W1-5/W1-6 插进本类；max_steps 本版只是涨停上限参数。
- 异常出口：LlmError 等分层异常本版原样上抛（main.py 兜底是 W1-7 的事），循环
  绝不吞错，也不做半路降级。
- 事件：run_step_started / ticket_appended(经 store) / run_ended 由本循环/store
  触发；exit_passed|failed 属 W2-1 出场三验，这里先不发。
- 三问投影从账本还原（target=tasks.goal、objective/success_criteria=runs.config_json）：
  断电续跑（W1-5）时同一条还原路径就能用，不依赖内存状态。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel

from secpt.llms.client import Goal, StepOutput
from secpt.llms.main_llm import MainLlm
from secpt.ob.events import EventBus
from secpt.store import attention, ledger
from secpt.store.ledger import RunRow, TaskRow, TicketObserver


class ToolOutput(BaseModel):
    """dispatcher 单次调用回执：小票四要素由工具适配器自己署名（§3.1 raw_ref 由它定锚位）。"""

    tool_name: str
    args: dict[str, str]
    exit_code: int | None
    summary: str
    raw_ref: str


class Dispatcher(Protocol):
    """工具分派口（W1-4 的 registry 将实现它；loop 只依赖协议，不 import tools 层）。"""

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
        """进场 + 主循环直到 done 或步线耗尽。中线预算/水位/出场纪律由后续 W 卡插入。"""
        task, run = self.enter(user_input)
        outcomes: list[StepOutcome] = []
        finished = False
        for step_no in range(1, max_steps + 1):
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
        """WF2 单步：打包 → step → 落账 → 判 done。返回 (落账快照, 是否收工)。"""
        if self._bus is not None:
            self._bus.emit("run_step_started", task_id=task.id, run_id=run.id, step_no=step_no)
        pkg = attention.build(
            goal=self._goal_projection(task, run),
            step_no=step_no,
            plan_summary="（M1 主循环暂无规划重排，A/B/C 规划接线在 M2）",
            recent=ledger.recent_tickets(self._conn, task.id),
        )
        output = self._llm.step(pkg)
        step = ledger.record_step(
            self._conn, task.id, run.id, step_no, output.model_dump_json(exclude_none=True)
        )
        ticket_count = 0
        ticket_bus = self._ticket_bus()
        for call in output.tool_calls:
            result = self._dispatcher.dispatch(call.name, {k: str(v) for k, v in call.args.items()})
            ledger.append_ticket(
                self._conn,
                task.id,
                run.id,
                step.id,
                result.tool_name,
                result.args,
                result.exit_code,
                result.summary,
                result.raw_ref,
                bus=ticket_bus,
            )
            ticket_count += 1
        self._audit(task.id, f"step {step_no}: {output.thought}, tool_calls={ticket_count}")
        outcome = StepOutcome(
            step_no=step_no, step_id=step.id, output=output, ticket_count=ticket_count
        )
        return outcome, output.done

    def _ticket_bus(self) -> TicketObserver | None:
        """把 EventBus 转成 TicketObserver 回调签名（emit 收 kwargs、回调只收一个 Mapping）。"""
        if self._bus is None:
            return None

        def forward(name: str, payload: Mapping[str, object]) -> None:
            self._bus.emit(name, **payload)

        return forward

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
