"""M4-5 每步工具上限（先测试）：超限截断 + 落账 + 不进失速计。

任务书口径（M4-5）：
- 主循环每步 tool_calls 超过上限（默认 4，可配）时截断；
- 超限部分落账（不进失速计、不静默丢弃）——audit 必须点名每个被截断的工具；
- 截断的调用连 dispatch 都不发生（小票是执行结果的记录，没执行就没有小票）。

两层限流的分工（为什么测试要鸭子注入）：
- LLM 契约层：StepOutput.tool_calls 的 pydantic max_length=4（M1 命根，不动），
  FakeLLM 的 JSON 输出发不出 5 个以上调用；
- 主循环截断面（本卡新增）：防的是"LLM 层约束失效/放宽"的场景——
  _ScriptedMain 鸭子类绕过 JSON schema 直接构造 6 连 StepOutput 才能打到它；
  FakeLLM 真实协议路径用 limit=2（4 连截 2）单独覆盖一条，证明两层协同。
  若未来有人放宽 schema max_length，这里的第一道测试立即拦住越权执行。

失速计区分度（反事实锚）：StallCounter N=5、每张失败回执 +1——
limit=4 时一步 6 个失败调用只 +4 连败（<5 不拉铃，run 正常收束 normal）；
若实现漏截断（把 6 个全 dispatch），+6 立即数满拉铃 → E_STALL → stalled。
"normal 出口 + dispatcher 只见 4 个调用"这对断言同时锁死截断与失速计隔离。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from secpt.core.loop import PentestLoop, ToolOutput
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import Goal, LlmClient, StepOutput, ToolCall
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import migrate

GOAL_JSON = json.dumps(
    {
        "target": "10.0.0.5",
        "objective": "确认暴露面",
        "success_criteria": "小票落账",
    },
    ensure_ascii=False,
)
_GOAL = Goal(target="10.0.0.5", objective="确认暴露面", success_criteria="小票落账")


def _step_json(thought: str, *, calls: list[dict[str, object]], done: bool) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": calls, "done": done},
        ensure_ascii=False,
    )


def _many_calls(n: int) -> list[ToolCall]:
    """一步 n 个 tool_calls：t1..tn（名字可读，audit 点名断言用）。"""
    return [ToolCall(name=f"t{i}", args={"seq": str(i)}) for i in range(1, n + 1)]


def _oversize_step(thought: str, n: int) -> StepOutput:
    """构造超 schema 的 n 连步（tool_calls max_length=4 是 LLM 契约层限流）。

    pydantic 校验在构造时即拦截 5+ 项——这恰是契约层的价值；但主循环截断面
    防的正是"契约层失效"的场景（未来放宽 schema / 其他构造路径），所以这里用
    model_construct（pydantic 官方跳过校验 API）造出 6 连步打到它。
    """
    return StepOutput.model_construct(
        thought=thought, plan_updates=[], tool_calls=_many_calls(n), done=False
    )


class _ScriptedMain:
    """绕过 JSON schema 的鸭子 MainLlm：手工 StepOutput 注入（测主循环截断面本身）。

    entrance/step 与 MainLlm 同手式（进场回 Goal、单步吐 StepOutput），
    一步可携带任意数量的 tool_calls——这是 LLM 契约层失效后主循环截断面
    必须独自挡住的输入形状。
    """

    def __init__(self, steps: list[StepOutput]) -> None:
        self._steps = list(steps)

    def entrance(self, user_input: str) -> Goal:
        return _GOAL

    def step(self, pkg: object) -> StepOutput:
        return self._steps.pop(0)


class FakeDispatcher:
    """全 Fake 执行器：署名固定回执；fail_all=True 时全部回执按失败计（失速计 +1）。"""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []
        self._fail_all = fail_all

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        self.calls.append((name, dict(args)))
        if self._fail_all:
            return ToolOutput(
                tool_name=name,
                args=dict(args),
                exit_code=None,
                summary="E_TOOL_FAIL: 注入失败（失速计区分度用）",
                raw_ref="",
            )
        return ToolOutput(
            tool_name=name,
            args=dict(args),
            exit_code=0,
            summary=f"{name} 完成",
            raw_ref=f"runs/fake/{name}.txt",
        )


def _make(
    steps: list[StepOutput],
    *,
    step_tool_limit: int = 4,
    fail_all: bool = False,
) -> SimpleNamespace:
    """最小 rig（无 bus）：鸭子 MainLlm + step_tool_limit 注入 PentestLoop 构造器。"""
    conn = migrate.connect(":memory:")
    dispatcher = FakeDispatcher(fail_all=fail_all)
    loop = PentestLoop(
        conn,
        _ScriptedMain(steps),  # type: ignore[arg-type]
        dispatcher,
        exit_checker=ExitChecklist(),
        step_tool_limit=step_tool_limit,
    )
    return SimpleNamespace(conn=conn, loop=loop, dispatcher=dispatcher)


def _truncation_audits(conn) -> list[str]:
    rows = conn.execute(
        "SELECT what FROM audit_log WHERE what LIKE '%工具上限截断%' ORDER BY id"
    ).fetchall()
    return [str(r["what"]) for r in rows]


def test_step_tool_limit_truncates_and_audits() -> None:
    """一步 6 个调用 limit=4：只 dispatch 前 4 个；超限 2 个在 audit 点名（不静默丢弃）。"""
    t = _make(
        [_oversize_step("一口气六连", 6), StepOutput(thought="收工", done=True)],
        step_tool_limit=4,
    )
    result = t.loop.run("测试主机 10.0.0.5")
    assert result.finished_naturally is True
    # 截断：执行器只见前 4 个调用，小票也只落 4 张
    assert [name for name, _ in t.dispatcher.calls] == ["t1", "t2", "t3", "t4"]
    assert t.conn.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"] == 4
    # 落账：超限部分逐个点名
    audits = _truncation_audits(t.conn)
    assert len(audits) == 1
    assert "t5" in audits[0] and "t6" in audits[0]
    assert "未执行 2 个" in audits[0]
    assert "6 个 tool_calls" in audits[0]


def test_step_tool_limit_default_four_keeps_all() -> None:
    """默认上限 4：恰好 4 个调用不截断、不落截断账（旧脚本行为守恒）。"""
    t = _make(
        [
            StepOutput(thought="恰好四连", tool_calls=_many_calls(4)),
            StepOutput(thought="收工", done=True),
        ]
    )
    result = t.loop.run("测试主机 10.0.0.5")
    assert result.finished_naturally is True
    assert len(t.dispatcher.calls) == 4
    assert _truncation_audits(t.conn) == []


def test_step_tool_limit_configurable_to_two_via_llm_json_path() -> None:
    """上限可配（limit=2）且走真实 LLM 协议路径：FakeLLM JSON 4 连被主循环截 2。

    LLM 契约层 max_length=4 允许 4 连过 schema，主循环截断面把执行面再压到 2——
    两层协同、各管各的（契约层管"说得出口"，主循环管"执行得下去"）。
    """
    conn = migrate.connect(":memory:")
    fake = FakeLLM(
        [
            GOAL_JSON,
            _step_json(
                "四连但上限二",
                calls=[{"name": f"t{i}", "args": {"seq": str(i)}} for i in range(1, 5)],
                done=False,
            ),
            _step_json("收工", calls=[], done=True),
        ]
    )
    dispatcher = FakeDispatcher()
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        dispatcher,
        exit_checker=ExitChecklist(),
        step_tool_limit=2,
    )
    result = loop.run("测试主机 10.0.0.5")
    assert result.finished_naturally is True
    assert [name for name, _ in dispatcher.calls] == ["t1", "t2"]
    audits = _truncation_audits(conn)
    assert len(audits) == 1
    assert "t3" in audits[0] and "t4" in audits[0]
    assert "未执行 2 个" in audits[0]


def test_truncated_calls_never_feed_stall_counter() -> None:
    """截断的调用不进失速计：6 个失败调用 limit=4 → 连败只 +4（<5 不拉铃）。

    反事实：若漏截断把 6 个全 dispatch，第 5 张失败回执就数满 N=5 拉铃 →
    coach 缺位 raise E_STALL → exit_kind=stalled。normal 出口即证明失速计
    只吃了 4 个（截断的 2 个连收数资格都没有）。
    """
    t = _make(
        [_oversize_step("六连全炸", 6), StepOutput(thought="收工", done=True)],
        step_tool_limit=4,
        fail_all=True,
    )
    result = t.loop.run("测试主机 10.0.0.5")
    assert result.finished_naturally is True
    assert result.exit_kind == "normal"  # +4 < 5：截断让失速计永不数满
    assert len(t.dispatcher.calls) == 4
    assert len(_truncation_audits(t.conn)) == 1


def test_step_tool_limit_must_be_positive() -> None:
    """构造期契约：step_tool_limit <= 0 直接 ValueError（截断面不允许裸奔）。"""
    with pytest.raises(ValueError, match="step_tool_limit"):
        PentestLoop(
            migrate.connect(":memory:"),
            _ScriptedMain([]),
            FakeDispatcher(),
            exit_checker=ExitChecklist(),
            step_tool_limit=0,
        )
