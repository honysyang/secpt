"""事件驱动 Planner：监听黑板变更，自动触发规划。

借鉴 ARTEX 设计：
- 黑板变更 → Debounce → Planner 唤醒 → 生成意图 → Frontier
- 减少无效轮询，事件驱动架构
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from harness.logging.logger import get_logger

logger = get_logger(__name__)


class IntentStatus(StrEnum):
    """意图状态。"""

    PENDING = "pending"  # 等待认领
    RUNNING = "running"  # 执行中
    COMPLETED = "completed"  # 完成
    FAILED = "failed"  # 失败
    CANCELLED = "cancelled"  # 取消


@dataclass
class Intent:
    """一个可执行的工作单元。"""

    id: str
    description: str
    agent_role: str  # 由哪个角色执行（coder/researcher/...）
    priority: int = 0  # 优先级（高优先先执行）
    status: IntentStatus = IntentStatus.PENDING
    created_at: float = field(default_factory=time.time)
    claimed_by: str | None = None  # 认领者 ID
    claimed_at: float | None = None
    result: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FrontierStats:
    """意图队列统计。"""

    pending: int
    running: int
    completed: int
    failed: int


class Frontier:
    """意图队列：管理待执行的工作单元。

    特性：
    - 优先级排序（高优先先出）
    - 原子认领（防止重复认领）
    - 状态追踪
    """

    def __init__(self) -> None:
        """初始化意图队列。"""
        self._intents: dict[str, Intent] = {}
        self._pending: asyncio.PriorityQueue[tuple[int, str]] = asyncio.PriorityQueue()
        self._lock = asyncio.Lock()
        self._counter = 0

    async def submit(self, intent: Intent) -> None:
        """提交新意图。

        Args:
            intent: 要提交的意图
        """
        async with self._lock:
            self._counter += 1
            if not intent.id:
                intent.id = f"intent_{self._counter}"
            self._intents[intent.id] = intent
            await self._pending.put((-intent.priority, intent.id))  # 负数实现高优先先出

    async def claim(self, worker_id: str) -> Intent | None:
        """认领一个意图（原子操作）。

        Args:
            worker_id: Worker ID

        Returns:
            认领的意图，无可用时返回 None
        """
        while True:
            try:
                _, intent_id = self._pending.get_nowait()
            except asyncio.QueueEmpty:
                return None

            async with self._lock:
                intent = self._intents.get(intent_id)
                if intent is None or intent.status != IntentStatus.PENDING:
                    continue  # 已被认领或取消，取下一个

                intent.status = IntentStatus.RUNNING
                intent.claimed_by = worker_id
                intent.claimed_at = time.time()
                return intent

    async def complete(self, intent_id: str, result: Any) -> None:
        """标记意图完成。"""
        async with self._lock:
            if intent := self._intents.get(intent_id):
                intent.status = IntentStatus.COMPLETED
                intent.result = result

    async def fail(self, intent_id: str, error: str) -> None:
        """标记意图失败。"""
        async with self._lock:
            if intent := self._intents.get(intent_id):
                intent.status = IntentStatus.FAILED
                intent.error = error

    async def cancel(self, intent_id: str) -> None:
        """取消意图。"""
        async with self._lock:
            if intent := self._intents.get(intent_id):
                intent.status = IntentStatus.CANCELLED

    async def get_stats(self) -> FrontierStats:
        """获取队列统计。"""
        async with self._lock:
            by_status: dict[str, int] = {}
            for intent in self._intents.values():
                by_status[intent.status] = by_status.get(intent.status, 0) + 1
            return FrontierStats(
                pending=by_status.get(IntentStatus.PENDING, 0),
                running=by_status.get(IntentStatus.RUNNING, 0),
                completed=by_status.get(IntentStatus.COMPLETED, 0),
                failed=by_status.get(IntentStatus.FAILED, 0),
            )

    async def get_pending_intents(self) -> list[Intent]:
        """获取所有待处理意图。"""
        async with self._lock:
            return [
                i for i in self._intents.values()
                if i.status == IntentStatus.PENDING
            ]

    async def get_all_intents(self) -> list[Intent]:
        """获取所有意图。"""
        async with self._lock:
            return list(self._intents.values())


class EventDrivenPlanner:
    """事件驱动 Planner：监听黑板变更，自动生成执行计划。

    工作流程：
    1. 黑板写入关键信息 → 触发事件
    2. Debounce 聚合（避免频繁触发）
    3. Planner 分析黑板状态
    4. 生成 0..N 个意图 → Frontier
    5. Worker 从 Frontier 认领执行
    """

    def __init__(
        self,
        blackboard: Any,  # HierarchicalBlackboard 或 Blackboard
        llm: Any,
        debounce_ms: int = 500,
        max_intents_per_wake: int = 5,
        available_roles: list[str] | None = None,
    ) -> None:
        """初始化事件驱动 Planner。

        Args:
            blackboard: 黑板实例
            llm: LLM 客户端
            debounce_ms: 防抖毫秒数
            max_intents_per_wake: 每次唤醒最多生成意图数
            available_roles: 可用角色名单。不传时 LLM 会编造/遗漏角色，
                             导致意图无法路由到真实智能体
        """
        self.blackboard = blackboard
        self.llm = llm
        self.debounce_ms = debounce_ms
        self.max_intents_per_wake = max_intents_per_wake
        self.available_roles = available_roles or []

        self.frontier = Frontier()
        self._pending_triggers: asyncio.Task | None = None
        self._running = False
        self._wake_count = 0

        # 订阅黑板事件
        self._setup_blackboard_subscription()

    def _setup_blackboard_subscription(self) -> None:
        """订阅黑板变更事件。"""
        if hasattr(self.blackboard, 'subscribe'):
            # HierarchicalBlackboard
            self.blackboard.subscribe("global:*", self._on_blackboard_event)
            self.blackboard.subscribe("domain:*", self._on_blackboard_event)
            self.blackboard.subscribe("critical:*", self._on_blackboard_event)
        elif hasattr(self.blackboard, 'on'):
            # 基础 Blackboard
            self.blackboard.on("*", self._on_blackboard_event)

    def _on_blackboard_event(self, event: Any) -> None:
        """黑板事件回调（防抖触发）。"""
        if self._pending_triggers is not None and not self._pending_triggers.done():
            # 已有待处理的触发，取消（重置防抖计时器）
            self._pending_triggers.cancel()

        self._pending_triggers = asyncio.create_task(self._debounced_wake())

    async def _debounced_wake(self) -> None:
        """防抖唤醒：等待一段时间无新事件后真正执行。

        该任务运行在后台，异常若不在此捕获记录，会成为
        "Task exception was never retrieved" 的孤儿异常，
        规划失败的根因将被静默丢失，因此必须显式记录日志。
        """
        try:
            await asyncio.sleep(self.debounce_ms / 1000)
            await self.wake()
        except asyncio.CancelledError:
            pass  # 新的触发已安排
        except Exception:
            logger.exception("debounce_wake_failed")

    async def wake(self, task_description: str | None = None) -> list[Intent]:
        """唤醒 Planner：分析黑板，生成意图。

        Args:
            task_description: 可选的当前用户任务。黑板上下文只含 key 名，
                              不含值——不显式传入任务，Planner 无法知道
                              要规划什么（CLI 集成时的关键缺口）。

        Returns:
            本次生成的意图列表
        """
        self._wake_count += 1

        # 收集黑板状态
        context = await self._gather_blackboard_context(task_description)

        # 调用 LLM 分析
        analysis = await self._analyze_with_llm(context)

        # 解析并生成意图
        intents = self._parse_intents(analysis)

        # 提交到 Frontier
        for intent in intents[:self.max_intents_per_wake]:
            await self.frontier.submit(intent)

        return intents

    async def _gather_blackboard_context(self, task_description: str | None = None) -> str:
        """收集黑板上下文摘要。"""
        parts = []

        # 显式任务优先（黑板里只有 key 名，不含值）
        if task_description:
            parts.append(f"[用户任务] {task_description}")

        # 全局层
        if hasattr(self.blackboard, 'get_critical_entries'):
            critical = await self.blackboard.get_critical_entries()
            if critical:
                parts.append(f"[关键信息] {len(critical)} 条")

        if hasattr(self.blackboard, '_global'):
            global_keys = list(self.blackboard._global.keys())[:5]
            if global_keys:
                parts.append(f"[全局] {', '.join(global_keys)}")

        # 领域层
        if hasattr(self.blackboard, '_domains'):
            domain_names = list(self.blackboard._domains.keys())
            if domain_names:
                parts.append(f"[领域] {', '.join(domain_names)}")

        # 统计
        if hasattr(self.blackboard, 'get_stats'):
            stats = await self.blackboard.get_stats()
            parts.append(f"[统计] {stats}")

        return "\n".join(parts)

    async def _analyze_with_llm(self, context: str) -> str:
        """调用 LLM 分析黑板状态，生成执行计划。"""
        roles_block = ""
        if self.available_roles:
            roles_block = (
                f"\n可用角色（必须从中选择，不得编造）: {', '.join(self.available_roles)}\n"
            )

        prompt = f"""你是一个任务规划器。根据当前黑板状态，生成下一步要执行的任务列表。

当前黑板状态：
{context}
{roles_block}
要求：
1. 每个意图必须是可执行的具体动作（发起请求、编写并运行脚本、读取文件、分析数据），不要生成"复核""评审""制定规范""进度跟踪"等管理类元任务
2. 第一个意图必须直接推进用户任务本身，而不是讨论怎么组织工作
3. 严格格式：每行一个，[优先级] 角色名: 任务描述（角色名只能用上面名单里的）
4. 按优先级排序，最多 5 个

示例：
[P0] pentest_recon: 对 https://example.com 发起 HTTP 请求，记录响应头中的安全配置
[P1] coder: 编写脚本枚举该站点的常见备份文件路径"""

        response = await self.llm.chat([
            {"role": "system", "content": "你是任务规划器，根据状态生成执行计划。"},
            {"role": "user", "content": prompt},
        ])
        return response["choices"][0]["message"]["content"]

    def _parse_intents(self, analysis: str) -> list[Intent]:
        """解析 LLM 输出为意图列表。"""
        intents = []
        for line in analysis.split("\n"):
            line = line.strip()
            if not line or not line.startswith("["):
                continue

            # 解析 [P0] coder: 任务描述
            try:
                priority_str, rest = line[1:].split("]", 1)
                priority = int(priority_str.replace("P", "").strip())
                role, description = rest.strip().split(":", 1)
                role = role.strip()
                description = description.strip()
                # 角色为空或格式残缺的行丢弃——空角色意图无法路由到任何专家
                if not role or not description:
                    continue
                intents.append(Intent(
                    id="",
                    description=description,
                    agent_role=role,
                    priority=priority,
                ))
            except (ValueError, IndexError):
                continue  # 跳过无法解析的行

        return intents

    async def run_until_idle(self, max_rounds: int = 10) -> FrontierStats:
        """运行直到 Frontier 清空或达到最大轮数。

        Args:
            max_rounds: 最大唤醒轮数

        Returns:
            Frontier 统计
        """
        for _ in range(max_rounds):
            stats = await self.frontier.get_stats()
            if stats.pending == 0 and stats.running == 0:
                break
            await self.wake()
            await asyncio.sleep(0.1)  # 给 Worker 执行时间

        return await self.frontier.get_stats()


class Worker:
    """Worker：从 Frontier 认领并执行意图。

    支持注入自定义执行器（executor）：注入后按角色路由到真实智能体
    （如 RoleRegistry 构造的 SubAgent，带工具与 ReAct 循环）；
    未注入或执行器返回 None 时，回退为内置的纯 LLM 单次执行。
    """

    def __init__(
        self,
        worker_id: str,
        llm: Any,
        tools: dict[str, Callable] | None = None,
        executor: Callable[[Intent], Any] | None = None,
    ) -> None:
        """初始化 Worker。

        Args:
            worker_id: Worker 唯一 ID
            llm: LLM 客户端
            tools: 可用工具字典 {name: callable}
            executor: 可选的意图执行器，签名 (intent) -> result；
                      返回 None 表示放弃执行，回退内置逻辑
        """
        self.worker_id = worker_id
        self.llm = llm
        self.tools = tools or {}
        self.executor = executor
        self._running = False
        self._completed: list[Intent] = []

    async def run_once(self, frontier: Frontier) -> Intent | None:
        """执行一次认领-执行循环。

        Args:
            frontier: 意图队列

        Returns:
            执行的意图，无可用时返回 None
        """
        intent = await frontier.claim(self.worker_id)
        if intent is None:
            return None

        try:
            result = await self._execute_intent(intent)
            await frontier.complete(intent.id, result)
            self._completed.append(intent)
            return intent
        except Exception as e:
            await frontier.fail(intent.id, str(e))
            return intent

    async def run_loop(self, frontier: Frontier, max_idle: float = 5.0) -> list[Intent]:
        """持续执行直到空闲。

        Args:
            frontier: 意图队列
            max_idle: 最大空闲秒数

        Returns:
            所有完成的意图
        """
        self._running = True
        idle_time = 0.0

        while self._running and idle_time < max_idle:
            intent = await self.run_once(frontier)
            if intent is None:
                idle_time += 0.1
                await asyncio.sleep(0.1)
            else:
                idle_time = 0.0

        return self._completed

    def stop(self) -> None:
        """停止 Worker。"""
        self._running = False

    async def _execute_intent(self, intent: Intent) -> Any:
        """执行单个意图：优先走注入的执行器，回退纯 LLM 执行。"""
        if self.executor is not None:
            result = await self.executor(intent)
            if result is not None:
                return result

        prompt = f"""执行任务：{intent.description}

你是 {intent.agent_role} 角色。请完成任务并返回结果。"""

        response = await self.llm.chat([
            {"role": "system", "content": f"你是 {intent.agent_role}，执行分配的任务。"},
            {"role": "user", "content": prompt},
        ])
        return response["choices"][0]["message"]["content"]


class EventDrivenEngine:
    """完整的事件驱动引擎：黑板 + Planner + Frontier + Workers。"""

    def __init__(
        self,
        blackboard: Any,
        llm: Any,
        num_workers: int = 3,
        debounce_ms: int = 500,
        executor: Callable[[Intent], Any] | None = None,
        available_roles: list[str] | None = None,
    ) -> None:
        """初始化引擎。

        Args:
            blackboard: 黑板实例
            llm: LLM 客户端
            num_workers: Worker 数量
            debounce_ms: Planner 防抖毫秒
            executor: 可选的意图执行器，透传给所有 Worker
            available_roles: 可用角色名单，透传给 Planner（约束意图的角色合法性）
        """
        self.blackboard = blackboard
        self.llm = llm
        self.num_workers = num_workers

        self.planner = EventDrivenPlanner(
            blackboard, llm, debounce_ms=debounce_ms, available_roles=available_roles
        )
        self.workers: list[Worker] = [
            Worker(f"worker_{i}", llm, executor=executor) for i in range(num_workers)
        ]

    async def run(
        self,
        max_rounds: int = 10,
        task_description: str | None = None,
        poll_interval: float = 0.5,
        settle_window: float = 2.0,
    ) -> dict[str, Any]:
        """运行引擎直到空闲。

        编排顺序（修复启动竞态的关键）：
        1. 先 wake 生成首批意图（LLM 调用有秒级网络延迟）
        2. 再启动 Workers——它们立即有活可认领，不会 idle 超时早退
        3. 轮询等待队列清空；清空后再等一个防抖窗口确认无新意图
           （Worker 执行会写黑板，订阅机制会自动触发后续 wake，
            因此这里绝不能反复手动 wake——那是轮询驱动，白烧 token）

        Args:
            max_rounds: 最大轮询次数（防止死等）
            task_description: 可选的用户任务，透传给首次 wake
            poll_interval: 轮询间隔秒
            settle_window: 队列清空后的确认窗口秒（覆盖防抖周期）

        Returns:
            执行统计（intents 含完整意图对象，供调用方提取结果）
        """
        # 1. 先生成首批意图（Planner LLM 失败不应炸掉整个引擎——
        #    记录错误后按"无意图"处理，Workers 正常启动等待黑板事件唤醒）
        try:
            intents = await self.planner.wake(task_description=task_description)
        except Exception:
            logger.exception("planner_initial_wake_failed")
            intents = []
        logger.info("planner_initial_wake", intent_count=len(intents))

        stats = await self.planner.frontier.get_stats()
        logger.info("frontier_after_wake", **vars(stats))

        # 2. 再启动 Workers（此刻队列里已有活）
        worker_tasks = [
            asyncio.create_task(w.run_loop(self.planner.frontier, max_idle=30.0))
            for w in self.workers
        ]

        # 3. 轮询等待：队列清空 + 确认窗口内无新意图（防抖 wake 需要时间）
        poll_round = 0
        for poll_round in range(1, max_rounds + 1):
            await asyncio.sleep(poll_interval)
            stats = await self.planner.frontier.get_stats()
            if stats.pending == 0 and stats.running == 0:
                await asyncio.sleep(settle_window)
                stats = await self.planner.frontier.get_stats()
                if stats.pending == 0 and stats.running == 0:
                    logger.info("frontier_settled", poll_rounds=poll_round)
                    break
            logger.info("frontier_poll", poll_rounds=poll_round, **vars(stats))

        # 停止 Workers
        for w in self.workers:
            w.stop()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        all_intents = [i for w in self.workers for i in w._completed]
        return {
            "frontier_stats": stats,
            "completed_intents": [
                {"id": i.id, "role": i.agent_role, "result_preview": str(i.result)[:100]}
                for w in self.workers for i in w._completed
            ],
            "wake_count": self.planner._wake_count,
            "intents": all_intents,
        }
