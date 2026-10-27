"""事件驱动 Planner 和 Frontier 测试。"""

import asyncio

import pytest

from runtime.event_driven import (
    EventDrivenEngine,
    EventDrivenPlanner,
    Frontier,
    Intent,
    IntentStatus,
    Worker,
)


class FakeLLM:
    """Fake LLM，返回预定义响应。"""

    def __init__(self, responses: list[str] | None = None):
        self.responses = responses or ["[P0] coder: 实现功能"]
        self.call_count = 0

    async def chat(self, messages, tools=None):
        self.call_count += 1
        response = self.responses[(self.call_count - 1) % len(self.responses)]
        return {
            "choices": [{"message": {"content": response}}],
            "usage": {"total_tokens": 10},
        }


class TestFrontier:
    """意图队列测试。"""

    async def test_submit_and_claim(self):
        """提交并认领意图。"""
        frontier = Frontier()
        intent = Intent(id="", description="测试任务", agent_role="coder", priority=1)
        await frontier.submit(intent)

        claimed = await frontier.claim("worker_1")
        assert claimed is not None
        assert claimed.id == intent.id
        assert claimed.status == IntentStatus.RUNNING
        assert claimed.claimed_by == "worker_1"

    async def test_priority_order(self):
        """高优先级先被认领。"""
        frontier = Frontier()

        await frontier.submit(Intent(id="low", description="低", agent_role="a", priority=1))
        await frontier.submit(Intent(id="high", description="高", agent_role="a", priority=5))
        await frontier.submit(Intent(id="mid", description="中", agent_role="a", priority=3))

        first = await frontier.claim("w")
        assert first.id == "high"

        second = await frontier.claim("w")
        assert second.id == "mid"

    async def test_no_double_claim(self):
        """同一意图不能被重复认领。"""
        frontier = Frontier()
        await frontier.submit(Intent(id="x", description="任务", agent_role="a"))

        first = await frontier.claim("worker_1")
        assert first is not None

        second = await frontier.claim("worker_2")
        assert second is None  # 队列已空

    async def test_complete_and_fail(self):
        """完成和失败状态流转。"""
        frontier = Frontier()
        await frontier.submit(Intent(id="ok", description="成功任务", agent_role="a"))
        await frontier.submit(Intent(id="fail", description="失败任务", agent_role="a"))

        ok_intent = await frontier.claim("w")
        await frontier.complete(ok_intent.id, "成功结果")

        fail_intent = await frontier.claim("w")
        await frontier.fail(fail_intent.id, "错误信息")

        stats = await frontier.get_stats()
        assert stats.completed == 1
        assert stats.failed == 1

    async def test_get_stats_empty(self):
        """空队列统计。"""
        frontier = Frontier()
        stats = await frontier.get_stats()
        assert stats.pending == 0
        assert stats.running == 0
        assert stats.completed == 0


class TestEventDrivenPlanner:
    """事件驱动 Planner 测试。"""

    @pytest.fixture
    def blackboard(self):
        """创建支持异步操作的黑板 Mock。"""
        class MockBB:
            async def get_critical_entries(self):
                return {}

            async def get_stats(self):
                return {"global_entries": 0}

            _global = {}
            _domains = {}

            def subscribe(self, pattern, callback):
                pass

        return MockBB()

    async def test_manual_wake_generates_intents(self, blackboard):
        """手动唤醒生成意图。"""
        llm = FakeLLM([
            "[P0] coder: 实现核心功能\n[P1] researcher: 调研需求",
        ])

        planner = EventDrivenPlanner(blackboard, llm)
        intents = await planner.wake()

        assert len(intents) == 2
        assert intents[0].agent_role == "coder"
        assert intents[0].priority == 0

    async def test_intents_submitted_to_frontier(self, blackboard):
        """生成的意图提交到 Frontier。"""
        llm = FakeLLM(["[P0] coder: 任务"])
        planner = EventDrivenPlanner(blackboard, llm)

        await planner.wake()
        stats = await planner.frontier.get_stats()
        assert stats.pending == 1

    async def test_parse_multiple_intents(self):
        """解析多行意图。"""
        bb = type("BB", (), {
            "subscribe": lambda s, p, c: None,
        })()

        llm = FakeLLM()
        planner = EventDrivenPlanner(bb, llm)

        analysis = """[P0] coder: 实现 API
[P1] tester: 编写测试
[P2] writer: 更新文档
无效行不应解析"""

        intents = planner._parse_intents(analysis)
        assert len(intents) == 3
        assert intents[0].priority == 0
        assert intents[1].agent_role == "tester"

    async def test_debounce_triggers_planner(self):
        """防抖机制触发 Planner。"""
        events_received = []

        # fake 方法必须与真实 HierarchicalBlackboard 契约一致：async 方法
        async def _critical_entries(self):
            return {}

        async def _stats(self):
            return {}

        bb = type("BB", (), {
            "get_critical_entries": _critical_entries,
            "_global": {"key": "value"},
            "_domains": {},
            "get_stats": _stats,
            "subscribe": lambda s, p, c: events_received.append(p),
        })()

        llm = FakeLLM(["[P0] coder: 任务"])
        planner = EventDrivenPlanner(bb, llm, debounce_ms=100)

        # 触发事件（模拟黑板写入）
        planner._on_blackboard_event(type("E", (), {"key": "global:test"})())

        # 等待防抖
        await asyncio.sleep(0.2)

        assert planner._wake_count >= 1


class TestWorker:
    """Worker 测试。"""

    async def test_worker_claims_and_executes(self):
        """Worker 认领并执行意图。"""
        frontier = Frontier()
        await frontier.submit(Intent(id="task1", description="写代码", agent_role="coder"))

        llm = FakeLLM(["代码实现完成"])
        worker = Worker("w1", llm)

        result = await worker.run_once(frontier)
        assert result is not None
        assert result.status == IntentStatus.COMPLETED
        assert "代码实现" in str(result.result)

    async def test_worker_loop_until_idle(self):
        """Worker 循环执行直到空闲。"""
        frontier = Frontier()
        for i in range(3):
            await frontier.submit(Intent(id=f"t{i}", description=f"任务{i}", agent_role="a"))

        llm = FakeLLM(["完成"])
        worker = Worker("w1", llm)

        completed = await worker.run_loop(frontier, max_idle=0.3)
        assert len(completed) == 3


class TestWorkerExecutor:
    """注入式执行器：CLI swarm 模式按角色路由到真实智能体（带工具）。"""

    async def test_executor_used_for_execution(self):
        """注入 executor 后走执行器而非内置 LLM 调用。"""
        calls: list[Intent] = []

        async def executor(intent: Intent) -> str:
            calls.append(intent)
            return f"由真实智能体执行: {intent.description}"

        frontier = Frontier()
        await frontier.submit(Intent(id="t1", description="扫描网站", agent_role="coder"))
        worker = Worker("w1", FakeLLM(), executor=executor)

        result = await worker.run_once(frontier)

        assert len(calls) == 1
        assert result.status == IntentStatus.COMPLETED
        assert "由真实智能体执行" in str(result.result)

    async def test_executor_none_falls_back_to_builtin(self):
        """executor 返回 None → 回退内置纯 LLM 执行。"""
        llm = FakeLLM(["内置执行结果"])

        async def executor(intent: Intent) -> None:
            return None

        frontier = Frontier()
        await frontier.submit(Intent(id="t1", description="任务", agent_role="a"))
        worker = Worker("w1", llm, executor=executor)

        result = await worker.run_once(frontier)

        assert llm.call_count == 1  # 回退到内置执行
        assert "内置执行结果" in str(result.result)

    async def test_no_executor_backward_compatible(self):
        """不传 executor（旧用法）行为不变。"""
        frontier = Frontier()
        await frontier.submit(Intent(id="t1", description="任务", agent_role="a"))
        worker = Worker("w1", FakeLLM(["旧逻辑结果"]))

        result = await worker.run_once(frontier)
        assert "旧逻辑结果" in str(result.result)

    async def test_engine_passes_task_to_planner(self):
        """Engine.run 的 task_description 进入 Planner 上下文（CLI 集成缺口回归）。"""
        captured: list[str] = []

        class RecordingLLM(FakeLLM):
            async def chat(self, messages, tools=None):
                captured.append(messages[-1]["content"])
                return {"choices": [{"message": {"content": "（无任务）"}}],
                        "usage": {"total_tokens": 1}}

        class MockBB:
            async def get_critical_entries(self):
                return {}

            async def get_stats(self):
                return {"entries": 0}

        engine = EventDrivenEngine(MockBB(), RecordingLLM(), num_workers=1)
        await engine.run(max_rounds=1, task_description="扫描 aimohu.com")

        assert any("[用户任务] 扫描 aimohu.com" in c for c in captured)

    def test_parse_intents_drops_empty_role(self):
        """角色为空的意图行丢弃（回归：CLI 曾出现 [空角色] 意图）。"""
        planner = EventDrivenPlanner.__new__(EventDrivenPlanner)  # 只测纯解析逻辑
        analysis = "\n".join([
            "[P0] coder: 编写扫描脚本",
            "[P1] : 角色缺失的任务",          # 空角色 → 丢弃
            "[P2] reviewer:",                  # 空描述 → 丢弃
            "[P3] pentest_recon: 基线探测",
        ])
        intents = planner._parse_intents(analysis)
        assert [i.agent_role for i in intents] == ["coder", "pentest_recon"]

    async def test_available_roles_in_prompt(self):
        """可用角色名单注入 Planner 提示词（回归：LLM 曾编造/遗漏角色）。"""
        captured: list[str] = []

        class RecordingLLM(FakeLLM):
            async def chat(self, messages, tools=None):
                captured.append(messages[-1]["content"])
                return {"choices": [{"message": {"content": "（无）"}}],
                        "usage": {"total_tokens": 1}}

        class MockBB:
            async def get_critical_entries(self):
                return {}

            async def get_stats(self):
                return {}

        planner = EventDrivenPlanner(
            MockBB(), RecordingLLM(), available_roles=["coder", "pentest_recon"]
        )
        await planner.wake(task_description="扫描站点")

        assert "可用角色" in captured[0]
        assert "coder, pentest_recon" in captured[0]


class TestEventDrivenEngine:
    """完整引擎集成测试。"""

    @pytest.fixture
    def blackboard(self):
        """创建支持异步操作的黑板 Mock。"""
        class MockBB:
            async def get_critical_entries(self):
                return {"crit": "alert"}

            async def get_stats(self):
                return {"global_entries": 1}

            _global = {"summary": "initial"}
            _domains = {"test": {}}

            def subscribe(self, pattern, callback):
                pass

        return MockBB()

    async def test_engine_runs_planner_and_workers(self, blackboard):
        """引擎运行 Planner 和 Workers。"""
        llm = FakeLLM([
            "[P0] coder: 实现功能 A\n[P1] tester: 测试功能 A",
            "[P0] writer: 写文档",  # 第二轮
        ])

        engine = EventDrivenEngine(blackboard, llm, num_workers=2, debounce_ms=50)
        result = await engine.run(max_rounds=3)

        assert "frontier_stats" in result
        assert "completed_intents" in result
        assert result["wake_count"] >= 1
