"""多智能体协作测试。"""

import pytest

from runtime.agents.base import SubAgent
from runtime.agents.collaboration import CollaborationResult, Debate, Parallel, Router


class FakeLLM:
    """模拟 LLM，根据提示词返回预设响应。"""

    def __init__(self, responses: dict[str, str] | list[str]):
        """初始化。

        Args:
            responses: 如果是 dict，根据关键词匹配响应；如果是 list，按顺序返回
        """
        self.responses = responses
        self.call_count = 0
        self.history: list[str] = []

    async def chat(self, messages, tools=None):
        prompt = messages[-1]["content"] if messages else ""
        self.history.append(prompt)
        self.call_count += 1

        # 找到 Thought 或 content
        content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                break

        if isinstance(self.responses, dict):
            # 关键词匹配
            for key, response in self.responses.items():
                if key in content:
                    return {
                        "choices": [{"message": {"content": response}}],
                        "usage": {"total_tokens": 10},
                    }
            # 默认返回第一个
            return {
                "choices": [{"message": {"content": list(self.responses.values())[0]}}],
                "usage": {"total_tokens": 10},
            }
        else:
            # 顺序返回
            idx = (self.call_count - 1) % len(self.responses)
            return {
                "choices": [{"message": {"content": self.responses[idx]}}],
                "usage": {"total_tokens": 10},
            }


def make_agent(name: str, llm: FakeLLM, instructions: str = "你是助手") -> SubAgent:
    """创建测试用 SubAgent。"""
    return SubAgent(name=name, instructions=instructions, llm=llm)


class TestSubAgent:
    async def test_run(self):
        llm = FakeLLM({"测试": "回答"})
        agent = make_agent("test", llm)

        result = await agent.run("测试")

        assert result.output == "回答"
        assert result.agent_name == "test"
        assert result.tokens_used == 10

    async def test_run_with_context(self):
        llm = FakeLLM({"上下文": "带上下文回答"})
        agent = make_agent("test", llm)

        result = await agent.run("测试", context="上下文信息")

        assert result.output == "带上下文回答"


class TestRouter:
    async def test_route_to_expert(self):
        coordinator_llm = FakeLLM({"任务": "coder"})  # 路由到 coder
        coordinator = make_agent("coordinator", coordinator_llm)

        coder_llm = FakeLLM({"任务": "代码结果"})
        coder = make_agent("coder", coder_llm)

        writer_llm = FakeLLM({"任务": "文本结果"})
        writer = make_agent("writer", writer_llm)

        router = Router(coordinator, {"coder": coder, "writer": writer})
        result = await router.route("任务")

        assert result.mode == "router"
        assert result.metadata["routed_to"] == "coder"
        assert "代码结果" in result.output

    async def test_route_fallback(self):
        # Coordinator 返回不存在的专家名
        coordinator_llm = FakeLLM({"任务": "unknown_expert"})
        coordinator = make_agent("coordinator", coordinator_llm)

        coder = make_agent("coder", FakeLLM({"任务": "代码"}))

        router = Router(coordinator, {"coder": coder})
        result = await router.route("任务")

        # 回退到 coordinator 自己处理
        assert result.metadata["routed_to"] == "coordinator"


class TestParallel:
    async def test_parallel_execute(self):
        agent1 = make_agent("agent1", FakeLLM({"任务": "结果1"}))
        agent2 = make_agent("agent2", FakeLLM({"任务": "结果2"}))

        parallel = Parallel([agent1, agent2])
        result = await parallel.execute("任务")

        assert result.mode == "parallel"
        assert len(result.agent_results) == 2
        assert "结果1" in result.output
        assert "结果2" in result.output

    async def test_parallel_with_aggregator(self):
        agent1 = make_agent("agent1", FakeLLM({"任务": "结果1"}))
        agent2 = make_agent("agent2", FakeLLM({"任务": "结果2"}))
        aggregator = make_agent("aggregator", FakeLLM({"汇总": "汇总结果"}))

        parallel = Parallel([agent1, agent2], aggregator=aggregator)
        result = await parallel.execute("任务")

        assert len(result.agent_results) == 3  # 2 执行 + 1 汇总
        assert result.output == "汇总结果"


class TestDebate:
    async def test_debate_flow(self):
        pro = make_agent("pro", FakeLLM(["支持观点", "支持反驳"]))
        con = make_agent("con", FakeLLM(["反对观点", "反对反驳"]))
        judge = make_agent("judge", FakeLLM(["裁决结果"]))

        debate = Debate(pro, con, judge, rounds=2)
        result = await debate.debate("主题")

        assert result.mode == "debate"
        assert result.output == "裁决结果"
        assert len(result.agent_results) == 5  # 2 轮 * (pro + con) + judge
        assert result.metadata["rounds"] == 2

    async def test_debate_single_round(self):
        pro = make_agent("pro", FakeLLM(["支持"]))
        con = make_agent("con", FakeLLM(["反对"]))
        judge = make_agent("judge", FakeLLM(["裁决"]))

        debate = Debate(pro, con, judge, rounds=1)
        result = await debate.debate("主题")

        assert len(result.agent_results) == 3  # pro + con + judge


class TestCollaborationResult:
    def test_result_structure(self):
        from runtime.agents.base import AgentResult

        result = CollaborationResult(
            output="最终输出",
            mode="test",
            agent_results=[AgentResult(output="a", agent_name="a")],
            metadata={"key": "value"},
        )

        assert result.output == "最终输出"
        assert result.mode == "test"
        assert len(result.agent_results) == 1
        assert result.metadata["key"] == "value"
