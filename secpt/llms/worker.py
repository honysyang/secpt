"""Worker（领单工人）的器官 facade（M3-5 拍板①③）。

为什么独立一个器官而不是复用 MainLlm：主 LLM 的发言对象是注意力包（黑板全
景），worker 的发言对象是 Mission 工单（单一任务纵深）——信息面不同、预算
面不同（步预算 ≤ 任务预算）、审计身份不同（actor=workerA 分层，蓝本 §4）。
两层共用同一套 client structured 纪律（规划档温度、E_SCHEMA 重试由协议层
天然承接）。

技能挂载（M5，设计文档 L11 + L42）：工单可带 ``skills`` 技能 id 清单——
worker 领单时从文件资产库读技能正文与其引用的纪律片段（角色 ← skills ←
prompts 的下链），拼进系统话术。缺件/引用断裂一律 audit 留痕后按"未挂载"
继续：技能是增强不是前置，但"没挂上"必须可证，绝不静默假装挂上。

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

from secpt.assets import load_role, load_skill, prompt_text, role_fragments, skill_fragments
from secpt.core.mission_board import Mission, MissionResult
from secpt.errors import SecptError
from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message, StepOutput
from secpt.llms.coach import Coach
from secpt.store import ledger
from secpt.tools.registry import ToolOutput, ToolRegistry


class Worker:
    """领单工人：工单驱动步循环 → 申报直写黑板 → 诚实收拢小票。"""

    def __init__(
        self,
        *,
        llm: LlmClient,
        dispatcher: ToolRegistry,
        conn: sqlite3.Connection,
        audit_actor: str = "workerA",
        coach: Coach | None = None,
    ) -> None:
        """llm/dispatcher/conn per-thread 独立装配，worker 不碰共享全局。

        ``coach``（M5-13）：可选的 LLMC 救场器。只有工单声明
        ``coach_after_failures > 0`` 且连续失败达标才会被调用一次；缺省 None
        = 该 worker 不接教练（M3-5 老语义）。
        """
        self._llm = llm
        self._dispatcher = dispatcher
        self._conn = conn
        self._actor = audit_actor
        self._coach = coach

    def run(self, mission: Mission) -> MissionResult:
        """领单干活：工单四个人事（goal/persona/skills/step_budget）定节奏，收拢小票交代。"""
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
        system_prompt, role_name = self._system_prompt(mission, task_id)
        role_note = f"，role={role_name}" if role_name else ""
        skills_note = f"，skills={','.join(mission.skills)}" if mission.skills else ""
        ledger.audit(
            self._conn,
            task_id,
            self._actor,
            f"领单：{mission.goal}（persona={mission.persona}{role_note}{skills_note}）",
        )
        findings_count = 0
        failures_streak = 0
        coach_used = False
        hint = ""
        for step_no in range(1, mission.step_budget + 1):
            hint_note = f"\n教练提示（一次性）：{hint}" if hint else ""
            hint = ""  # 提案吸收一次即用掉（同主循环口径）
            messages = [
                Message(role="system", content=system_prompt),
                Message(
                    role="user",
                    content=(
                        f"工单目标：{mission.goal}\n"
                        f"步预算 {mission.step_budget}（当前第 {step_no} 步）。\n"
                        f"已合法申报 findings {findings_count} 条。真做完就 done。{hint_note}"
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
                self._audit_hitl(task_id, receipt)
                findings_count += self._claim_finding(task_id, call.name, receipt)
                # 失败连败计（denied 不算工具失败：闸门没放行，工具没执行）。
                if not receipt.denied:
                    failures_streak = 0 if receipt.exit_code == 0 else failures_streak + 1
            # M5-13 教练接入（每单一次）：连续失败达工单门槛才请 LLMC 给一句换法。
            if (
                self._coach is not None
                and not coach_used
                and mission.coach_after_failures > 0
                and failures_streak >= mission.coach_after_failures
            ):
                coach_used = True
                hint = self._coach_hint(mission, task_id, failures_streak)
                if hint:
                    failures_streak = 0  # 换打法后重新起算（只影响是否再触发，名额已用）
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

    def _system_prompt(self, mission: Mission, task_id: str) -> tuple[str, str | None]:
        """基线话术 + 角色包（who+how）+ 工单挂载技能（正文 + 引用纪律片段）。

        角色解析口径（M5，设计文档 L11/L41）：``persona`` 若命中角色库 id，就把
        整包挂上（行事风格 + 角色引用的技能 + 纪律片段）；不命中则沿用自由文本
        persona（M3-5 老语义守恒，不阻断派单）。
        技能清单 = 角色引用的技能 ∪ 工单显式技能（保序去重）——工单可覆盖/追加。
        缺件与引用断裂都 audit 留痕后跳过——技能是增强不是前置，但"这单实际挂了
        哪几条"必须能从账本复算（领单行带 role=/skills=）。

        Returns:
            (系统话术, 命中的角色名或 None) —— 角色名供领单审计行如实署名。
        """
        parts = [prompt_text("worker.system")]
        role = load_role(mission.persona) if mission.persona else None
        skill_ids: list[str] = []
        if role is not None:
            parts.append(f"## 角色：{role.name}\n{role.description}\n{role.instruction}".strip())
            try:
                parts.extend(role_fragments(role))
            except SecptError as exc:
                ledger.audit(
                    self._conn,
                    task_id,
                    self._actor,
                    f"角色 {role.id} 引用纪律缺失：{exc}（角色其余部分照挂）",
                )
            skill_ids.extend(role.skills)
        skill_ids.extend(mission.skills)
        for skill_id in dict.fromkeys(skill_ids):
            skill = load_skill(skill_id)
            if skill is None:
                ledger.audit(
                    self._conn, task_id, self._actor, f"技能库缺件：{skill_id}（按未挂载继续）"
                )
                continue
            try:
                fragments = skill_fragments(skill)
            except SecptError as exc:
                ledger.audit(
                    self._conn,
                    task_id,
                    self._actor,
                    f"技能 {skill_id} 引用纪律缺失：{exc}（按未挂载继续）",
                )
                continue
            parts.append(f"## 挂载技能：{skill.name}\n{skill.description}\n{skill.body}".strip())
            parts.extend(fragments)
        return "\n\n".join(parts), (role.name if role is not None else None)

    def _coach_hint(self, mission: Mission, task_id: str, streak: int) -> str:
        """M5-13 教练接入：一次 LLMC 调用换一句可执行的换法。

        可降级纪律：任何异常/无话可说都只 audit(actor=workerA) 留痕并返回空串——
        worker 继续按自己的节奏干，绝不因为教练掉线而判本单失败。
        ``graphs`` 传工单现状（worker 没有独立双图）：LLMC 看到的是"在打什么 +
        连续失败几次"，与主循环救场共用同一套提案形状（idea/reason）。
        """
        if self._coach is None:
            return ""
        try:
            proposal = self._coach.propose(
                graphs=f"工单：{mission.goal}\n连续 {streak} 次工具调用失败",
                plan=mission.goal,
            )
        except Exception as exc:  # noqa: BLE001  可降级器官兜底（见 docstring）
            ledger.audit(self._conn, task_id, self._actor, f"教练接入降级：{exc}")
            return ""
        if proposal is None:
            ledger.audit(self._conn, task_id, self._actor, "教练接入：LLM 无话说（不改变本单节奏）")
            return ""
        ledger.audit(
            self._conn,
            task_id,
            self._actor,
            f"教练接入（只提案不裁决）：{proposal.idea}——{proposal.reason}",
        )
        return f"{proposal.idea}（原因：{proposal.reason}）"

    def _audit_hitl(self, task_id: str, receipt: ToolOutput) -> None:
        """M4-4 发起侧落账：HIGH 审批批/拒两路各有 audit 痕（actor=workerA）。

        与主循环同手式（loop L346 的 denied/hitl 双路）：worker 的 Guard 拒绝
        （含未装代理 fail-closed 与代理超时拒绝）和经代理批准在此各落一行——
        "workerA 发起 + 代理裁决"actor 分层的发起半边；裁决半边由 main.py
        的 sink（actor=main）在 serve 侧落。
        """
        if receipt.denied:
            ledger.audit(
                self._conn, task_id, self._actor, f"HIGH 审批发起（拒绝）: {receipt.summary}"
            )
        elif receipt.hitl_approved:
            ledger.audit(
                self._conn,
                task_id,
                self._actor,
                f"HIGH 审批发起（批准 {receipt.hitl_note}）: {receipt.tool_name}",
            )

    def _claim_finding(self, task_id: str, tool_name: str, receipt: ToolOutput) -> int:
        """declare_finding 受理即直写 claimed（黑板单向门）；其余工具一律不写账。"""
        if tool_name != "declare_finding" or receipt.exit_code != 0 or not receipt.raw_ref:
            return 0
        title = (receipt.args.get("title") or "").strip()
        detail = (receipt.args.get("detail") or "").strip()
        ledger.append_finding(
            self._conn, task_id, title or "(无题)", detail, receipt.raw_ref, actor=self._actor
        )
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
