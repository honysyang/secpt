"""协作模式：多智能体协作策略。

支持三种模式：
1. Router: 主 Agent 根据意图分发给子 Agent（Handoff）
2. Parallel: 多个子 Agent 并行执行，结果汇总
3. Debate: 多个子 Agent 辩论，最终由裁判裁决

支持共享黑板（Blackboard）协同：
- BlackboardRouter: 带黑板的路由模式
- BlackboardParallel: 带黑板的并行模式
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from runtime.agents.base import AgentResult, SubAgent
from runtime.blackboard import Blackboard, BlackboardAgent


@dataclass
class CollaborationResult:
    """协作结果。"""

    output: str
    mode: str
    agent_results: list[AgentResult]
    metadata: dict[str, Any] | None = None


class Router:
    """路由器模式：主 Agent 根据意图选择子 Agent。"""

    def __init__(
        self,
        coordinator: SubAgent,
        sub_agents: dict[str, SubAgent],
    ) -> None:
        self.coordinator = coordinator
        self.sub_agents = sub_agents

    async def route(self, task: str, context: str = "") -> CollaborationResult:
        """路由任务到合适的子 Agent。

        流程：
        1. Coordinator 分析任务，决定路由到哪个子 Agent
        2. 选中的子 Agent 执行任务
        3. 返回结果
        """
        # Coordinator 决定路由；带上一句话职责描述，
        # 角色池变大后仅凭名字无法准确路由
        expert_list = "\n".join(
            f"- {name}: {agent.description or '(无描述)'}"
            for name, agent in self.sub_agents.items()
        )
        route_prompt = (
            f"可用专家:\n{expert_list}\n"
            f"任务: {task}\n"
            f"请选择最合适的专家名称，只输出名称本身。"
        )
        route_result = await self.coordinator.run_simple(route_prompt, context)
        selected_name = route_result.output.strip()

        # 执行
        if selected_name in self.sub_agents:
            agent = self.sub_agents[selected_name]
            result = await agent.run(task, context)
            return CollaborationResult(
                output=result.output,
                mode="router",
                agent_results=[result],
                metadata={"routed_to": selected_name},
            )
        else:
            # 未匹配，Coordinator 自己处理
            result = await self.coordinator.run(task, context)
            return CollaborationResult(
                output=result.output,
                mode="router",
                agent_results=[result],
                metadata={"routed_to": "coordinator"},
            )


class Parallel:
    """并行模式：多个子 Agent 同时执行，结果汇总。"""

    def __init__(
        self,
        sub_agents: list[SubAgent],
        aggregator: SubAgent | None = None,
    ) -> None:
        self.sub_agents = sub_agents
        self.aggregator = aggregator

    async def execute(self, task: str, context: str = "") -> CollaborationResult:
        """并行执行任务。

        流程：
        1. 所有子 Agent 并行执行同一任务
        2. 可选：Aggregator 汇总结果
        """
        results = await asyncio.gather(
            *[agent.run(task, context) for agent in self.sub_agents]
        )

        # 汇总
        if self.aggregator:
            combined = "\n\n".join(
                f"[{r.agent_name}]\n{r.output}" for r in results
            )
            summary = await self.aggregator.run_simple(
                f"汇总以下结果:\n{combined}", context
            )
            final_output = summary.output
            all_results = [*results, summary]
        else:
            final_output = "\n\n".join(
                f"[{r.agent_name}]\n{r.output}" for r in results
            )
            all_results = list(results)

        return CollaborationResult(
            output=final_output,
            mode="parallel",
            agent_results=all_results,
            metadata={"agent_count": len(self.sub_agents)},
        )


class Debate:
    """辩论模式：正反两方辩论，裁判裁决。"""

    def __init__(
        self,
        pro_agent: SubAgent,
        con_agent: SubAgent,
        judge: SubAgent,
        rounds: int = 2,
    ) -> None:
        self.pro_agent = pro_agent
        self.con_agent = con_agent
        self.judge = judge
        self.rounds = rounds

    async def debate(self, topic: str, context: str = "") -> CollaborationResult:
        """执行辩论。

        流程：
        1. Pro 提出观点
        2. Con 反驳
        3. 重复 rounds 轮
        4. Judge 裁决
        """
        all_results: list[AgentResult] = []
        pro_args: list[str] = []
        con_args: list[str] = []

        for round_num in range(1, self.rounds + 1):
            # Pro 发言
            pro_prompt = (
                f"主题: {topic}\n"
                f"轮次: {round_num}\n"
                f"对方观点: {'; '.join(con_args) if con_args else '无'}\n"
                f"请提出支持观点。"
            )
            pro_result = await self.pro_agent.run_simple(pro_prompt, context)
            pro_args.append(pro_result.output)
            all_results.append(pro_result)

            # Con 发言
            con_prompt = (
                f"主题: {topic}\n"
                f"轮次: {round_num}\n"
                f"对方观点: {pro_result.output}\n"
                f"请提出反对观点。"
            )
            con_result = await self.con_agent.run_simple(con_prompt, context)
            con_args.append(con_result.output)
            all_results.append(con_result)

        # Judge 裁决
        judge_prompt = (
            f"主题: {topic}\n"
            f"正方观点:\n{'; '.join(pro_args)}\n"
            f"反方观点:\n{'; '.join(con_args)}\n"
            f"请裁决并总结。"
        )
        judge_result = await self.judge.run_simple(judge_prompt, context)
        all_results.append(judge_result)

        return CollaborationResult(
            output=judge_result.output,
            mode="debate",
            agent_results=all_results,
            metadata={
                "rounds": self.rounds,
                "pro_agent": self.pro_agent.name,
                "con_agent": self.con_agent.name,
            },
        )


class BlackboardRouter:
    """带共享黑板的路由模式。

    支持：
    - Coordinator 读取黑板上下文做路由决策
    - 子 Agent 读写黑板共享状态
    - 执行结果自动写回黑板
    """

    def __init__(
        self,
        coordinator: BlackboardAgent,
        sub_agents: dict[str, BlackboardAgent],
        blackboard: Blackboard,
    ) -> None:
        self.coordinator = coordinator
        self.sub_agents = sub_agents
        self.blackboard = blackboard

    async def route(self, task: str, context: str = "") -> CollaborationResult:
        """路由任务，集成黑板。"""
        # 写入任务到黑板
        await self.blackboard.write("current_task", task, agent="coordinator")

        # Coordinator 读取黑板上下文做决策
        bb_context = await self._gather_blackboard_context()
        full_context = f"{context}\n\n{bb_context}" if bb_context else context

        route_prompt = (
            f"可用专家: {', '.join(self.sub_agents.keys())}\n"
            f"任务: {task}\n"
            f"共享黑板状态:\n{bb_context}\n"
            f"请选择合适的专家名称，只输出名称。"
        )
        route_result = await self.coordinator.run_simple(route_prompt, full_context)
        selected_name = route_result.output.strip()

        # 写入路由决策
        await self.blackboard.write("routing_decision", selected_name, agent="coordinator")

        # 执行
        if selected_name in self.sub_agents:
            agent = self.sub_agents[selected_name]
            result = await agent.run(task, context)

            # 结果自动写回黑板（通过 BlackboardAgent）
            return CollaborationResult(
                output=result.output,
                mode="blackboard_router",
                agent_results=[result],
                metadata={
                    "routed_to": selected_name,
                    "blackboard_keys": await self.blackboard.list_keys(),
                },
            )
        else:
            result = await self.coordinator.run(task, context)
            return CollaborationResult(
                output=result.output,
                mode="blackboard_router",
                agent_results=[result],
                metadata={"routed_to": "coordinator"},
            )

    async def _gather_blackboard_context(self) -> str:
        """收集黑板上下文。"""
        keys = await self.blackboard.list_keys()
        if not keys:
            return ""

        parts = []
        for key in keys[:10]:  # 限制数量
            value = await self.blackboard.read(key)
            if value is not None:
                parts.append(f"[{key}]: {str(value)[:100]}...")

        return "\n".join(parts)


class BlackboardParallel:
    """带共享黑板的并行模式。

    支持：
    - 多个 Agent 并行执行
    - 实时读写黑板共享中间结果
    - 订阅其他 Agent 的进展
    - 最终汇总到黑板
    """

    def __init__(
        self,
        sub_agents: list[BlackboardAgent],
        blackboard: Blackboard,
        aggregator: BlackboardAgent | None = None,
    ) -> None:
        self.sub_agents = sub_agents
        self.blackboard = blackboard
        self.aggregator = aggregator

    async def execute(self, task: str, context: str = "") -> CollaborationResult:
        """并行执行，集成黑板协同。"""
        # 初始化黑板
        await self.blackboard.write("task", task, agent="system")
        await self.blackboard.write("status", "running", agent="system")
        await self.blackboard.write("agent_count", len(self.sub_agents), agent="system")

        # 并行执行（每个 Agent 独立读写黑板）
        results = await asyncio.gather(*[
            agent.run(task, context) for agent in self.sub_agents
        ])

        # 写入完成状态
        await self.blackboard.write("status", "completed", agent="system")

        # 汇总结果
        combined = "\n\n".join(
            f"[{r.agent_name}]\n{r.output}" for r in results
        )
        await self.blackboard.write("combined_results", combined, agent="system")

        # 可选：Aggregator 进一步处理
        if self.aggregator:
            # Aggregator 读取黑板上的综合结果
            bb_summary = await self.aggregator.run_simple(
                f"请基于以下结果生成最终摘要:\n{combined}",
                context
            )
            final_output = bb_summary.output
            await self.blackboard.write("final_output", final_output, agent="aggregator")
            all_results = [*results, bb_summary]
        else:
            final_output = combined
            all_results = list(results)

        # 收集黑板状态
        bb_keys = await self.blackboard.list_keys()
        bb_history = await self.blackboard.get_history()

        return CollaborationResult(
            output=final_output,
            mode="blackboard_parallel",
            agent_results=all_results,
            metadata={
                "agent_count": len(self.sub_agents),
                "blackboard_keys": bb_keys,
                "blackboard_events": len(bb_history),
            },
        )

    def subscribe_to_progress(self, callback: Any) -> None:
        """订阅任务进展。"""
        self.blackboard.subscribe("status", callback)
        self.blackboard.subscribe("current_task", callback)
