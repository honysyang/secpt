"""Worker（领单工人）的器官 facade（M3-5 拍板①③）。

为什么独立一个器官而不是复用 MainLlm：主 LLM 的发言对象是注意力包（黑板全
景），worker 的发言对象是 Mission 工单（单一任务纵深）——信息面不同、预算
面不同（步预算 ≤ 任务预算）、审计身份不同（actor=workerA 分层，蓝本 §4）。
两层共用同一套 client structured 纪律（规划档温度、E_SCHEMA 重试由协议层
天然承接）。

黑板纪律（拍板③）：worker 有独立 conn 直写 findings（唯一写口仍经
store/ledger 官门）——findings 与小票同生死的原子收口依然归主循环
step_transaction；worker 只走"申报受理 → 直写 claimed"单向门，绝不反串
tickets（那是主循环的收口权）。

降级纪律（拍板③）：任何异常（LLM 炸了 / 工具爆了 / 账本拒绝）都降级成
MissionResult(ok=False, finished=False) + audit 落痕（actor=workerA），绝
不静默吞错——板级 future 拿到的是"诚实的小票"而不是"沉默的异常"。

领单/收单 audit：领单（开工）与收单（终态）各一行，让"这单给过谁、结在哪"
账可证——多 worker 并行下这是唯一的排班真相。
"""

from __future__ import annotations

import sqlite3

from secpt.core.mission_board import Mission, MissionResult
from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message, StepOutput
from secpt.store import ledger
from secpt.tools.registry import ToolOutput, ToolRegistry

_SYS_PROMPT = (
    "你是一名单独领单的渗透测试工人（worker）。你每一步只能输出一个 JSON 对象："
    '{"thought": 一句理由, "plan_updates": [], '
    '"tool_calls": [{"name": 工具名, "args": {参数: 值}}] (≤4), "done": 是否收工}。'
    "有凭据（evidence_ref 指向真实产物文件）就申报 declare_finding；做完就 done。"
    "不要输出 JSON 以外的任何文本。"
)


class Worker:
    """领单工人：工单驱动步循环 → 申报直写黑板 → 诚实收拢小票。"""

    def __init__(
        self,
        *,
        llm: LlmClient,
        dispatcher: ToolRegistry,
        conn: sqlite3.Connection,
        audit_actor: str = "workerA",
    ) -> None:
        """llm/dispatcher/conn per-thread 独立装配，worker 不碰共享全局。"""
        self._llm = llm
        self._dispatcher = dispatcher
        self._conn = conn
        self._actor = audit_actor

    def run(self, mission: Mission) -> MissionResult:
        """领单干活：工单三个人事（goal/persona/step_budget）定节奏，收拢小票交代。"""
        task_id = mission.task_id
        if not task_id:
            # 订单锚点在 board 层就该钉上；这里兜底诚实拒绝（不假写飞账）。
            return MissionResult(
                mission_id=mission.mission_id,
                ok=False,
                finished=False,
                steps_taken=0,
                findings_count=0,
                summary="E_TOOL_FAIL: mission 缺 task_id 锚点，worker 不接悬空单",
            )
        ledger.audit(
            self._conn, task_id, self._actor, f"领单：{mission.goal}（persona={mission.persona}）"
        )
        findings_count = 0
        for step_no in range(1, mission.step_budget + 1):
            messages = [
                Message(role="system", content=_SYS_PROMPT),
                Message(
                    role="user",
                    content=(
                        f"工单目标：{mission.goal}\n"
                        f"步预算 {mission.step_budget}（当前第 {step_no} 步）。\n"
                        f"已合法申报 findings {findings_count} 条。真做完就 done。"
                    ),
                ),
            ]
            try:
                output = self._llm.structured(messages, StepOutput, temperature=TEMPERATURE_PLAN)
            except Exception as exc:  # noqa: BLE001  # 含 LlmError/TimeoutError等
                return self._degrade(
                    mission, task_id, exc, steps_so_far=step_no - 1, findings_count=findings_count
                )
            for call in output.tool_calls:
                receipt = self._dispatcher.dispatch(
                    call.name, {k: str(v) for k, v in call.args.items()}
                )
                findings_count += self._claim_finding(task_id, call.name, receipt)
            if output.done:
                summary = output.thought.strip() or "worker 收工（无 thought）"
                ledger.audit(
                    self._conn, task_id, self._actor, f"收单：{summary}；申报 {findings_count} 条"
                )
                return MissionResult(
                    mission_id=mission.mission_id,
                    ok=True,
                    finished=True,
                    steps_taken=step_no,
                    findings_count=findings_count,
                    summary=summary,
                )
        # 步预算告罄：没出错也没干完——ok=True（诚实区分"失败"与"未完成"）。
        summary = f"预算用尽：{mission.step_budget} 步内未收工，申报 {findings_count} 条"
        ledger.audit(self._conn, task_id, self._actor, f"收单（未完工）：{summary}")
        return MissionResult(
            mission_id=mission.mission_id,
            ok=True,
            finished=False,
            steps_taken=mission.step_budget,
            findings_count=findings_count,
            summary=summary,
        )

    def _claim_finding(self, task_id: str, tool_name: str, receipt: ToolOutput) -> int:
        """declare_finding 受理即直写 claimed（黑板单向门）；其余工具一律不写账。"""
        if tool_name != "declare_finding" or receipt.exit_code != 0 or not receipt.raw_ref:
            return 0
        title = (receipt.args.get("title") or "").strip()
        detail = (receipt.args.get("detail") or "").strip()
        ledger.append_finding(self._conn, task_id, title or "(无题)", detail, receipt.raw_ref)
        return 1

    def _degrade(
        self,
        mission: Mission,
        task_id: str,
        exc: Exception,
        *,
        steps_so_far: int,
        findings_count: int,
    ) -> MissionResult:
        """收纳异常：audit 落实录、小票带原因——"炸了但账有痕"。"""
        reason = str(exc) or exc.__class__.__name__
        ledger.audit(
            self._conn,
            task_id,
            self._actor,
            f"worker 降级：{reason}（步 {steps_so_far}/{mission.step_budget}）",
        )
        return MissionResult(
            mission_id=mission.mission_id,
            ok=False,
            finished=False,
            steps_taken=steps_so_far,
            findings_count=findings_count,
            summary=f"worker 降级：{reason}",
        )


__all__ = ["Worker"]
