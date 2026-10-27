"""人机协作增强：steer_work 和 side_questions。

借鉴 ARTEX 设计：
- steer_work: 运行时动态调整 Agent 行为
- side_questions: Agent 主动向人类提问
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


class InterventionType(StrEnum):
    """干预类型。"""

    STEER = "steer"  # 调整方向
    PAUSE = "pause"  # 暂停
    RESUME = "resume"  # 恢复
    CANCEL = "cancel"  # 取消
    INJECT_CONTEXT = "inject_context"  # 注入上下文
    CHANGE_GOAL = "change_goal"  # 改变目标


@dataclass
class Intervention:
    """干预请求。"""

    id: str
    type: InterventionType
    target_agent: str  # 目标 Agent
    message: str  # 干预内容
    created_at: float = field(default_factory=time.time)
    applied: bool = False
    result: Any | None = None


@dataclass
class SideQuestion:
    """边问：Agent 向人类提问。"""

    id: str
    agent: str  # 提问的 Agent
    question: str  # 问题内容
    context: str  # 提问背景
    priority: int = 0  # 优先级
    created_at: float = field(default_factory=time.time)
    answered: bool = False
    answer: str | None = None


class HumanInterventionManager:
    """人工干预管理器。

    支持：
    - steer_work: 动态调整运行中的 Agent
    - side_questions: Agent 主动提问
    - 实时干预队列
    """

    def __init__(self) -> None:
        """初始化干预管理器。"""
        self._interventions: dict[str, Intervention] = {}
        self._questions: dict[str, SideQuestion] = {}
        self._agent_callbacks: dict[str, Callable] = {}  # agent_id -> callback
        self._question_callbacks: list[Callable] = []  # 新提问回调
        self._counter = 0
        self._lock = asyncio.Lock()

    def register_agent(
        self,
        agent_id: str,
        steer_callback: Callable[[str], None],
    ) -> None:
        """注册 Agent 的干预回调。

        Args:
            agent_id: Agent ID
            steer_callback: 收到干预时的回调函数
        """
        self._agent_callbacks[agent_id] = steer_callback

    def on_question(self, callback: Callable[[SideQuestion], None]) -> None:
        """注册新提问回调。

        Args:
            callback: 收到新问题时调用
        """
        self._question_callbacks.append(callback)

    # ==================== steer_work ====================

    async def steer(
        self,
        target_agent: str,
        message: str,
        type: InterventionType = InterventionType.STEER,
    ) -> Intervention:
        """向运行中的 Agent 发送干预。

        Args:
            target_agent: 目标 Agent ID
            message: 干预内容
            type: 干预类型

        Returns:
            创建的干预记录
        """
        async with self._lock:
            self._counter += 1
            intervention = Intervention(
                id=f"int_{self._counter}",
                type=type,
                target_agent=target_agent,
                message=message,
            )
            self._interventions[intervention.id] = intervention

        # 通知 Agent
        if callback := self._agent_callbacks.get(target_agent):
            try:
                result = callback(message)
                if asyncio.iscoroutine(result):
                    result = await result
                intervention.result = result
                intervention.applied = True
            except Exception as e:
                intervention.result = f"Error: {e}"

        return intervention

    async def pause_agent(self, target_agent: str, reason: str = "") -> Intervention:
        """暂停 Agent。"""
        return await self.steer(
            target_agent,
            f"PAUSE: {reason}" if reason else "PAUSE",
            InterventionType.PAUSE,
        )

    async def resume_agent(self, target_agent: str) -> Intervention:
        """恢复 Agent。"""
        return await self.steer(
            target_agent,
            "RESUME",
            InterventionType.RESUME,
        )

    async def cancel_agent(self, target_agent: str, reason: str = "") -> Intervention:
        """取消 Agent。"""
        return await self.steer(
            target_agent,
            f"CANCEL: {reason}" if reason else "CANCEL",
            InterventionType.CANCEL,
        )

    async def inject_context(
        self,
        target_agent: str,
        context: str,
    ) -> Intervention:
        """向 Agent 注入上下文。"""
        return await self.steer(
            target_agent,
            context,
            InterventionType.INJECT_CONTEXT,
        )

    # ==================== side_questions ====================

    async def ask(
        self,
        agent: str,
        question: str,
        context: str = "",
        priority: int = 0,
    ) -> SideQuestion:
        """Agent 向人类提问。

        Args:
            agent: 提问的 Agent
            question: 问题
            context: 背景
            priority: 优先级

        Returns:
            创建的问题
        """
        async with self._lock:
            self._counter += 1
            q = SideQuestion(
                id=f"q_{self._counter}",
                agent=agent,
                question=question,
                context=context,
                priority=priority,
            )
            self._questions[q.id] = q

        # 通知监听者
        for callback in self._question_callbacks:
            try:
                result = callback(q)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

        return q

    async def answer(self, question_id: str, answer: str) -> SideQuestion | None:
        """回答 Agent 的提问。

        Args:
            question_id: 问题 ID
            answer: 答案

        Returns:
            更新后的问题
        """
        async with self._lock:
            if q := self._questions.get(question_id):
                q.answered = True
                q.answer = answer
                return q
            return None

    async def wait_for_answer(
        self,
        question_id: str,
        timeout: float = 300,
    ) -> str | None:
        """等待问题被回答。

        Args:
            question_id: 问题 ID
            timeout: 超时秒数

        Returns:
            答案，超时返回 None
        """
        start = time.time()
        while time.time() - start < timeout:
            async with self._lock:
                if q := self._questions.get(question_id):
                    if q.answered:
                        return q.answer
            await asyncio.sleep(0.5)
        return None

    # ==================== 查询 ====================

    async def get_pending_questions(self) -> list[SideQuestion]:
        """获取未回答的问题。"""
        async with self._lock:
            return [
                q for q in self._questions.values()
                if not q.answered
            ]

    async def get_interventions_for_agent(
        self,
        agent_id: str,
        limit: int = 10,
    ) -> list[Intervention]:
        """获取 Agent 的干预历史。"""
        async with self._lock:
            return [
                i for i in self._interventions.values()
                if i.target_agent == agent_id
            ][:limit]


class SteerableAgent:
    """可干预的 Agent：支持运行时动态调整。"""

    def __init__(
        self,
        agent_id: str,
        instructions: str,
        llm: Any,
        intervention_manager: HumanInterventionManager,
    ) -> None:
        """初始化。

        Args:
            agent_id: Agent ID
            instructions: 基础指令
            llm: LLM 客户端
            intervention_manager: 干预管理器
        """
        self.agent_id = agent_id
        self.instructions = instructions
        self.llm = llm
        self.intervention_manager = intervention_manager

        # 运行时状态
        self._paused = False
        self._cancelled = False
        self._injected_context: list[str] = []
        self._current_task: str | None = None

        # 注册干预回调
        intervention_manager.register_agent(agent_id, self._handle_intervention)

    async def _handle_intervention(self, message: str) -> str:
        """处理干预消息。"""
        if message.startswith("PAUSE"):
            self._paused = True
            return "已暂停"
        elif message == "RESUME":
            self._paused = False
            return "已恢复"
        elif message.startswith("CANCEL"):
            self._cancelled = True
            return "已取消"
        else:
            # 注入上下文或调整方向
            self._injected_context.append(message)
            return f"已接收: {message[:50]}"

    async def run(self, task: str, max_wait: float = 3600) -> Any:
        """执行任务，支持干预。

        Args:
            task: 任务描述
            max_wait: 最大等待时间（暂停时）

        Returns:
            执行结果
        """
        self._current_task = task
        self._cancelled = False

        while not self._cancelled:
            # 检查暂停
            if self._paused:
                await asyncio.sleep(1)
                continue

            # 构建提示（包含注入的上下文）
            prompt = self._build_prompt(task)

            # 调用 LLM
            response = await self.llm.chat([
                {"role": "system", "content": self.instructions},
                {"role": "user", "content": prompt},
            ])

            return response["choices"][0]["message"]["content"]

        return "任务已取消"

    def _build_prompt(self, task: str) -> str:
        """构建提示，包含注入的上下文。"""
        parts = [f"任务：{task}"]

        if self._injected_context:
            parts.append("\n补充上下文：")
            for ctx in self._injected_context[-3:]:  # 只保留最近 3 条
                parts.append(f"- {ctx}")

        return "\n".join(parts)

    def pause(self) -> None:
        """手动暂停。"""
        self._paused = True

    def resume(self) -> None:
        """手动恢复。"""
        self._paused = False

    def cancel(self) -> None:
        """手动取消。"""
        self._cancelled = True
