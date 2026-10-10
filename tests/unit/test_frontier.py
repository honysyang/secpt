"""B4 planner/frontier 测试：队列语义 / 深度优先 / 主循环收敛。"""

from __future__ import annotations

import asyncio
import json

from secpt.core.orchestrator.frontier import Frontier, build_intent_objective, run_frontier


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestFrontier:
    def test_add_claim_settle(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        i1 = fr.add("打入口A", priority=5, depth=0)
        i2 = fr.add("深入A1", priority=5, depth=1)
        i3 = fr.add("打入口B", priority=8, depth=0)
        # 认领顺序：priority DESC → depth DESC → 先创建先领
        first = fr.claim_next()
        assert first is not None and first.id == i3  # priority 8 最先
        second = fr.claim_next()
        assert second is not None and second.id == i2  # 同 priority，depth 1 优先（深度优先）
        assert second.attempts == 1
        fr.settle(second.id, status="done", result="拿到 shell")
        assert fr.count_open() == 1  # i1 仍 open
        res = fr.recent_results()
        assert res and res[0]["result"] == "拿到 shell"

    def test_claim_empty(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        assert fr.claim_next() is None


class _PlannerLLM:
    """第一轮产 2 意图，之后产空（收敛）。"""

    def __init__(self, rounds_json: list[str]):
        self._rounds = list(rounds_json)
        self.calls = 0

    async def call(self, messages, tools, model):
        self.calls += 1

        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Resp:
            def __init__(self, c):
                self.choices = [type("c", (), {"message": _Msg(c)})()]

        body = self._rounds.pop(0) if self._rounds else "[]"
        return _Resp(body)


class _WorkerLLM:
    """每条 intent：1 次 bash 后按意图标题决定是否 ANSWER。"""

    async def call(self, messages, tools, model):
        system = messages[0]["content"]
        answered = getattr(self, "_n", 0)
        self._n = answered + 1
        if self._n % 2 == 0 or "admin" in system:
            return _answer_resp("PWNED")
        return _tool_resp(self._n)


def _tool_resp(i):
    class _Fn:
        name, arguments = "bash", json.dumps({"command": "ls"})

    class _TC:
        id = f"tc{i}"
        function = _Fn()

    class _Msg:
        content = f"step {i}"
        tool_calls = [_TC()]
        reasoning_content = None

    class _Resp:
        choices = [type("c", (), {"message": _Msg()})()]

    return _Resp()


def _answer_resp(flag):
    class _Msg:
        content = f"ANSWER: {flag}"
        tool_calls = []
        reasoning_content = None

    class _Resp:
        choices = [type("c", (), {"message": _Msg()})()]

    return _Resp()


class TestRunFrontier:
    def test_full_loop_converges(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        plan_json = json.dumps(
            [
                {
                    "title": "侦察目标",
                    "goal": "摸清指纹",
                    "done_criteria": "有指纹",
                    "priority": 5,
                    "depth": 0,
                },
                {
                    "title": "打admin后台",
                    "goal": "拿flag",
                    "done_criteria": "ANSWER flag",
                    "priority": 9,
                    "depth": 1,
                },
            ],
            ensure_ascii=False,
        )
        planner = _PlannerLLM([plan_json])
        summary = _run(
            run_frontier(
                "目标：拿下 test",
                llm=_WorkerLLM(),
                planner_llm=planner,
                base_job_id="ht1",
                max_rounds=6,
                frontier=Frontier(tmp_path / "fr.db"),
            )
        )
        assert summary["mode"] == "frontier"
        # admin intent priority 9 先执行并拿到 flag → 整体收敛
        assert summary["flags"] == ["PWNED"]
        statuses = {c["intent"]: c["status"] for c in summary["chains"]}
        assert statuses.get("打admin后台") == "done"

    def test_intent_objective_shape(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        iid = fr.add("测试某接口", goal="拿到敏感数据", done_criteria="响应含 secret")
        intent = fr.claim_next()
        assert intent is not None
        obj = build_intent_objective("总目标", intent, fr.recent_results())
        assert "当前意图" in obj and "完成判据" in obj and "总目标" in obj
