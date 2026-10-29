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
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from secpt.assets import load_role, load_skill, prompt_text, role_fragments, skill_fragments
from secpt.core.dispatch import dispatch_batch
from secpt.core.mission_board import Mission, MissionResult
from secpt.errors import LedgerError, SecptError
from secpt.llms.client import TEMPERATURE_PLAN, LlmClient, Message, StepOutput
from secpt.llms.coach import Coach
from secpt.store import graph, ledger
from secpt.tools.registry import ToolOutput, ToolRegistry

# 收拢摘要带出的小票条数（M5-27）：够主 LLM 判断"跑成了什么"，又不把摘要撑成日志。
_RECENT_NOTES = 3
# 回执记忆条数（M5-31）：随每步 user 消息进包的最近回执上限——worker 的"我刚干了
# 什么"只在这里；真实 run 实测没有它，worker 会在同一 URL/目录上反复空转 6 步。
_HISTORY_NOTES = 8


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
        observer: Callable[[str, Mapping[str, object]], None] | None = None,
        parallel_dispatch: bool = False,
    ) -> None:
        """llm/dispatcher/conn per-thread 独立装配，worker 不碰共享全局。

        ``coach``（M5-13）：可选的 LLMC 救场器。只有工单声明
        ``coach_after_failures > 0`` 且连续失败达标才会被调用一次；缺省 None
        = 该 worker 不接教练（M3-5 老语义）。
        ``observer``（M5-29）：可选事件订阅口（装配层传 EventBus.emit）——worker
        过程此前对 CLI 不可见，只能等收拢摘要；接上后每步/每张票都能上屏。
        观测是旁观者：回调异常一律吞掉，绝不影响干活。
        ``parallel_dispatch``（M5-31）：worker 内部的批派单开关——复用主循环同一条
        保守批口径（:func:`secpt.core.dispatch.dispatch_batch`），全批 parallel_safe
        且开关开启才并发。默认 False（串行守恒）；装配层在探针不在场时开启
        （探针后端是进程级全局态，并发开窗会串账）。
        """
        self._llm = llm
        self._dispatcher = dispatcher
        self._conn = conn
        self._actor = audit_actor
        self._coach = coach
        self._observer = observer
        self._parallel_dispatch = parallel_dispatch

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
        notes: list[str] = []  # 最近 _RECENT_NOTES 张小票摘要（收拢时带出，M5-27）
        # M5-31 回执记忆（本轮修的失忆根因）：worker 的每步 user 消息此前只有
        # 工单目标/步数/申报数——自己上一步拿到什么回执完全看不见，同一 URL 反复
        # 抓、同一目录反复列（真实 run 实测 worker 10 步里 6 步在重复昨天的动作）。
        # 最近 _HISTORY_NOTES 张回执随消息进包，"我刚干了什么"从此在场。
        history: list[str] = []
        run_id = self._current_run_id(task_id)
        started = time.monotonic()
        for step_no in range(1, mission.step_budget + 1):
            step_started = time.monotonic()
            self._emit(
                "worker_step_started",
                actor=self._actor,
                task_id=task_id,
                mission_id=mission.mission_id,
                step_no=step_no,
                step_budget=mission.step_budget,
                goal=mission.goal[:80],
            )
            hint_note = f"\n教练提示（一次性）：{hint}" if hint else ""
            hint = ""  # 提案吸收一次即用掉（同主循环口径）
            history_note = ""
            if history:
                history_note = "\n最近工具回执（新→旧；重复同一动作前先看这里）：\n" + "\n".join(
                    f"- {line}" for line in history[:_HISTORY_NOTES]
                )
            messages = [
                Message(role="system", content=system_prompt),
                Message(
                    role="user",
                    content=(
                        f"工单目标：{mission.goal}\n"
                        f"步预算 {mission.step_budget}（当前第 {step_no} 步）。\n"
                        f"已合法申报 findings {findings_count} 条。真做完就 done。"
                        f"{history_note}{hint_note}"
                    ),
                ),
            ]
            try:
                output = self._llm.structured(messages, StepOutput, temperature=TEMPERATURE_PLAN)
            except Exception as exc:  # noqa: BLE001  # 含 LlmError/TimeoutError等
                return self._degrade(
                    mission,
                    task_id,
                    exc,
                    steps_so_far=step_no - 1,
                    findings_count=findings_count,
                    started=started,
                )
            # M5-31 批派单：与主循环共用保守批口径——全批 parallel_safe 且开关开启
            # 才并发（回执按提交序回填），混批/开关关整批串行，落账顺序确定性不变。
            calls = output.tool_calls
            receipts = dispatch_batch(
                self._dispatcher, calls, parallel_allowed=self._parallel_dispatch
            )
            for call, receipt in zip(calls, receipts, strict=True):
                self._record_ticket(task_id, run_id, step_no, call.name, call.args, receipt)
                # M5-29 worker 过程可见：每张票实时上屏（含 actor/耗时），
                # 不必等收拢摘要才知道这张单在干什么。
                self._emit(
                    "worker_ticket",
                    actor=self._actor,
                    task_id=task_id,
                    mission_id=mission.mission_id,
                    step_no=step_no,
                    tool_name=call.name,
                    exit_code=receipt.exit_code,
                    summary=receipt.summary[:120],
                    duration_ms=receipt.duration_ms,
                )
                # M5-27 收拢摘要带小票事实：主循环只看得到 summary，worker 的自述
                # thought 无法核实；带上真实回执，主 LLM 才判断得出这单跑成了什么。
                note = f"{call.name} exit={receipt.exit_code}: {receipt.summary[:80]}"
                notes.append(note)
                del notes[:-_RECENT_NOTES]
                # M5-31 回执记忆：本步回执进 history，下一步的 user 消息带上。
                history.insert(0, note)
                del history[_HISTORY_NOTES:]
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
            self._emit(
                "worker_step_ended",
                actor=self._actor,
                task_id=task_id,
                mission_id=mission.mission_id,
                step_no=step_no,
                duration_ms=int((time.monotonic() - step_started) * 1000),
                thought=output.thought[:100],
            )
            if output.done:
                summary = self._with_recent(
                    output.thought.strip() or "worker 收工（无 thought）", notes
                )
                ledger.audit(
                    self._conn, task_id, self._actor, f"收单：{summary}；申报 {findings_count} 条"
                )
                self._emit_mission_ended(
                    mission, started, steps=step_no, findings=findings_count, finished=True
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
        summary = self._with_recent(
            f"预算用尽：{mission.step_budget} 步内未收工，申报 {findings_count} 条", notes
        )
        ledger.audit(self._conn, task_id, self._actor, f"收单（未完工）：{summary}")
        self._emit_mission_ended(
            mission, started, steps=mission.step_budget, findings=findings_count, finished=False
        )
        return MissionResult(
            mission_id=mission.mission_id,
            ok=True,
            finished=False,
            steps_taken=mission.step_budget,
            findings_count=findings_count,
            summary=summary,
        )

    def _emit(self, name: str, **payload: object) -> None:
        """发一条观测事件（可降级器官：observer 缺位/抛错都不影响干活）。"""
        if self._observer is None:
            return
        try:
            self._observer(name, payload)
        except Exception:  # noqa: BLE001  观测失败绝不打断 worker
            return

    def _emit_mission_ended(
        self, mission: Mission, started: float, *, steps: int, findings: int, finished: bool
    ) -> None:
        """收单事件：多 worker 并行时，这是"这单跑了多久/跑成什么"的唯一实时真相。"""
        self._emit(
            "worker_mission_ended",
            actor=self._actor,
            mission_id=mission.mission_id,
            ok=finished,
            finished=finished,
            steps=steps,
            findings=findings,
            duration_ms=int((time.monotonic() - started) * 1000),
            summary=mission.goal[:80],
        )

    @staticmethod
    def _with_recent(summary: str, notes: Sequence[str]) -> str:
        """收拢摘要追加最近小票（M5-27）：让"这单干了什么"有回执事实可核。

        为什么不做成独立字段：主循环只把 ``MissionResult.summary`` 写成
        agent_run 小票的 summary，格式对齐最省事；只用 thought 自述会让
        "嘴上说完工"与"真跑成了"无法区分（实测出现过 worker 自述写盘、
        却零申报零产物的情况）。
        """
        if not notes:
            return summary
        return f"{summary}；最近小票：{' / '.join(notes)}"

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
        # M5-28 工具签名进 worker 话术：与主链同一个 registry 渲染，不另写副本。
        # 实测 worker 会因为看不到清单而点名 bash/shell/http_get（54 张小票里 49
        # 张是"未知工具"）——"能派什么"必须写在它眼前，而不是让它猜。
        catalog = self._tool_catalog()
        if catalog:
            parts.append(
                "## 可用工具（只能用下列工具；名字与参数必须逐字一致，猜错会被拒）\n"
                + "\n".join(f"- {line}" for line in catalog)
            )
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
            missing = self._missing_tools(skill.tools)
            if missing:
                # M5-28 技能与工具面一致性：技能教的是 shell 命令、工具面却没有，
                # 不提示就会变成"照着做但每步被拒"。审计留痕 + 现话术提示双份。
                ledger.audit(
                    self._conn,
                    task_id,
                    self._actor,
                    f"技能 {skill_id} 需要的工具未登记：{missing}（按缺件提示继续）",
                )
                parts.append(
                    f"注意：技能「{skill.name}」需要的工具未登记：{'、'.join(missing)}——"
                    "不要尝试这些工具（会被拒），改用已登记工具，或如实说明无法执行。"
                )
        return "\n\n".join(parts), (role.name if role is not None else None)

    def _tool_catalog(self) -> list[str]:
        """当前 worker 可用的工具签名行（registry 唯一持有 manifest）。"""
        catalog_of = getattr(self._dispatcher, "tool_catalog", None)
        return catalog_of() if callable(catalog_of) else []

    def _missing_tools(self, required: Sequence[str]) -> list[str]:
        """技能声明所需工具里，本 worker 没登记的那些（空 = 全可用）。"""
        if not required:
            return []
        manifests_of = getattr(self._dispatcher, "manifests", None)
        available = (
            {manifest.name for manifest in manifests_of()} if callable(manifests_of) else set()
        )
        return [name for name in required if name not in available]

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

    def _current_run_id(self, task_id: str) -> str | None:
        """worker 工具动作归属的 run：最近一次 run（无则小票记不上，落审计）。

        为什么让 worker 自己查：派单只带 task 锚；run 是"本次执行"的容器，
        用最近 run 与任务板 task_resolver 同口径，不额外引入板级参数。
        """
        try:
            return ledger.latest_run(self._conn, task_id).id
        except LedgerError:
            ledger.audit(self._conn, task_id, self._actor, "工单缺 run 锚点：本轮工具动作不落小票")
            return None

    def _record_ticket(
        self,
        task_id: str,
        run_id: str | None,
        step_no: int,
        tool_name: str,
        args: Mapping[str, Any],
        receipt: ToolOutput,
    ) -> None:
        """worker 的工具动作落小票（M5-25）：证据链不能在派单侧断掉。

        没有小票 → R1 找不到"票面证据源"，worker 的 finding 只能靠 R2/R3/R4
        验真。step_id 用 ``worker-<actor>-<step>`` 命名（worker 的步不进主循环
        水位线，也不写 steps 表；出场①的孤儿检查据此排除）。
        """
        if run_id is None:
            return
        ledger.append_ticket(
            self._conn,
            task_id,
            run_id,
            f"worker-{self._actor}-{step_no}",
            tool_name,
            {str(k): str(v) for k, v in args.items()},
            receipt.exit_code,
            receipt.summary,
            receipt.raw_ref,
            bus=None,
        )

    def _claim_finding(self, task_id: str, tool_name: str, receipt: ToolOutput) -> int:
        """declare_finding 受理即直写 claimed（黑板单向门）；其余工具一律不写账。

        M5-31 重复申报拦截：同名 claimed 已在账就不新增（与主循环同口径）——
        worker 的工单也会因为"想让主循环看见"而反复申报同一结论；拦截有 audit 痕。
        """
        if tool_name != "declare_finding" or receipt.exit_code != 0 or not receipt.raw_ref:
            return 0
        title = (receipt.args.get("title") or "").strip()
        detail = (receipt.args.get("detail") or "").strip()
        dup = ledger.pending_duplicate(self._conn, task_id, title or "(无题)", receipt.raw_ref)
        if dup is not None:
            ledger.audit(
                self._conn,
                task_id,
                self._actor,
                f"重复申报拦截：{title[:60]!r} 与已悬置 finding {dup.id[:8]}"
                " 同名，未新增（补新证据或改 title）",
            )
            return 0
        row = ledger.append_finding(
            self._conn, task_id, title or "(无题)", detail, receipt.raw_ref, actor=self._actor
        )
        # M5-15 证据图：worker 直写的 finding 也要有节点（否则出场①一致性会缺一条）。
        graph.put_finding_node(
            self._conn,
            task_id,
            row.id,
            title=row.title,
            evidence_ref=row.evidence_ref,
            actor=self._actor,
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
        started: float,
    ) -> MissionResult:
        """收纳异常：audit 落实录、小票带原因——"炸了但账有痕"。"""
        reason = str(exc) or exc.__class__.__name__
        ledger.audit(
            self._conn,
            task_id,
            self._actor,
            f"worker 降级：{reason}（步 {steps_so_far}/{mission.step_budget}）",
        )
        self._emit_mission_ended(
            mission,
            started,
            steps=steps_so_far,
            findings=findings_count,
            finished=False,
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
