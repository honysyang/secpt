"""v5 改造单测：任务理解归指挥官首轮唤醒 / 派单三要素（截止·注意事项·自执行）/ goal 落盘。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from secpt.core.orchestrator.commander import commander_round
from secpt.core.orchestrator.frontier import (
    Frontier,
    build_intent_objective,
    frontier_db_path,
    run_frontier,
)
from secpt.core.orchestrator.goals import (
    GoalSpec,
    goal_path,
    is_rendered,
    load_goal,
    render_goal_system,
    save_goal,
)
from secpt.core.orchestrator.hypothesis import HypothesisTree, hypothesis_db_path


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
    return tmp_path


class _ScriptedLLM:
    def __init__(self, payloads):
        self._p = list(payloads)
        self.calls = 0

    async def call(self, messages, tools, model):
        self.calls += 1
        body = json.dumps(self._p.pop(0) if self._p else {"tasks": []}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])


def _doc(direction: str) -> str:
    return (
        "## 当前阶段\n侦察\n## 主攻方向\n"
        + direction
        + "\n## 明确不做\n无\n## 转向日志\n- 初始"
    )


_GOAL_PAYLOAD = {
    "goal": {
        "objective": "拿下 test 靶机的 flag",
        "constraints": ["仅限授权范围", "不得删库"],
        "asset_scope": ["http://test.local"],
        "attack_steps": ["侦察指纹", "找注入点", "拿 flag"],
    },
    "hypotheses": [
        {
            "title": "入口存在 SQL 注入",
            "statement": "登录接口参数未过滤，可联合查询拖库",
            "test": "注入 ' or 1=1-- 返回绕过即证实",
            "confidence": 0.6,
        }
    ],
    "decision": "continue",
    "tasks": [
        {
            "title": "侦察指纹",
            "goal": "识别技术栈",
            "done_criteria": "说出框架与版本",
            "priority": 8,
            "max_wall_s": 120,
            "notes": "只做被动识别，不要打 payload",
            "self": False,
        },
        {
            "title": "确认入口可达",
            "goal": "curl 首页返回 200",
            "done_criteria": "HTTP 200",
            "priority": 9,
            "self": True,
            "notes": "只发一个请求",
        },
    ],
    "strategy": _doc("入口注入面"),
}


def test_commander_need_goal_parses_goal_spec(env) -> None:
    """首轮唤醒 need_goal=True：响应中的 goal 字段解析为 GoalSpec 挂到 outcome。"""
    fr = Frontier(frontier_db_path("g1"))
    tree = HypothesisTree(hypothesis_db_path("g1"))
    outcome = _run(
        commander_round(
            "客户原始需求：帮我测一下这个站",
            fr,
            tree,
            _ScriptedLLM([dict(_GOAL_PAYLOAD)]),
            need_goal=True,
        )
    )
    assert isinstance(outcome.goal, GoalSpec)
    assert "拿下 test 靶机" in outcome.goal.objective
    assert outcome.goal.constraints == ["仅限授权范围", "不得删库"]
    assert outcome.goal.asset_scope == ["http://test.local"]
    assert len(outcome.goal.attack_steps) == 3
    # 派单照常发生
    assert outcome.n_tasks == 2
    assert outcome.hypotheses_added == 1


def test_dispatch_carries_deadline_notes_self(env) -> None:
    """派单三要素落库：max_wall_s（截止）/ notes（注意事项）进 intents 列；self 进明细。"""
    fr = Frontier(frontier_db_path("g2"))
    tree = HypothesisTree(hypothesis_db_path("g2"))
    outcome = _run(
        commander_round(
            "目标",
            fr,
            tree,
            _ScriptedLLM([dict(_GOAL_PAYLOAD)]),
            need_goal=True,
        )
    )
    rows = {r["title"]: r for r in fr.list_all(status="open")}
    recon = rows["侦察指纹"]
    assert recon["max_wall_s"] == 120
    assert "不要打 payload" in recon["notes"]
    detail = {d["title"]: d for d in outcome.tasks_detail}
    assert detail["侦察指纹"]["max_wall_s"] == 120
    assert detail["确认入口可达"]["self"] is True
    assert detail["确认入口可达"]["iid"]  # 自执行路径按 id 自取，必须回传 iid
    # 操作员简报可见注意事项与截止（按 id 取侦察任务，避免认领顺序干扰）
    intent = fr.claim_by_id(detail["侦察指纹"]["iid"])
    obj = build_intent_objective("总目标", intent, fr.recent_results())
    assert "注意事项：" in obj and "不要打 payload" in obj
    assert "时限：" in obj and "120 秒" in obj


def test_claim_by_id(env) -> None:
    """按 id 自取（指挥官自执行路径）：仅 open 可领，并发语义与 claim_next 相同。"""
    fr = Frontier(frontier_db_path("g3"))
    iid = fr.add("自执行任务", goal="g", done_criteria="c")
    assert fr.claim_by_id(iid) is not None
    assert fr.claim_by_id(iid) is None  # 已 running，不可重复领
    assert fr.claim_next() is None  # 队列已空
    fr.settle(iid, status="done")
    assert fr.claim_by_id(iid) is None  # done 也不可领


def test_goal_roundtrip(env) -> None:
    """goal.json 落盘/读取（resume 不重复拆解的数据源）。"""
    goal = GoalSpec(objective="o", constraints=["c1"], asset_scope=["a1"], attack_steps=["s1"])
    p = save_goal("g4", goal)
    assert p == goal_path("g4") and p.is_file()
    loaded = load_goal("g4")
    assert loaded == goal
    assert load_goal("no-such-job") is None
    # 渲染产物过 is_rendered 校验（resume 后不再触发指挥官理解）
    assert is_rendered(render_goal_system(loaded))


def test_frontier_first_wake_understands_and_self_executes(env, monkeypatch) -> None:
    """端到端：入口原文 → 指挥官首轮产 goal（落盘）→ 派单三要素 → self 任务当场自执行。"""
    calls: list[str] = []

    def fake_run_engine(**kw):
        calls.append(kw.get("objective", ""))
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag="FLAG{v5_ok}",
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "g5"
    fr = Frontier(frontier_db_path(base))
    payloads = [dict(_GOAL_PAYLOAD)] + [
        {"decision": "continue", "tasks": [], "strategy": _doc("入口注入面")}
        for _ in range(6)
    ]
    summary = _run(
        run_frontier(
            "客户原始需求：帮我拿下这个测试站",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_ScriptedLLM(payloads),
            max_rounds=8,
            max_intents=10,
            stop_on_first_flag=False,  # 两条任务都要执行到（拿到 flag 不提前收敛）
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    # 1) 首轮理解产物落盘，resume 可直接续用
    goal = load_goal(base)
    assert goal is not None and "拿下 test 靶机" in goal.objective
    # 2) 结构化 objective 已进入执行简报（engine 收到的 objective 含渲染标记/资产段）
    assert any(is_rendered(o) or "【任务目标】" in o for o in calls)
    # 3) self 任务被指挥官当场执行（不占操作员等待）：两条任务都有执行记录
    statuses = {c["intent"]: c["status"] for c in summary["chains"]}
    assert statuses.get("确认入口可达") == "done"
    assert statuses.get("侦察指纹") == "done"
    assert summary["open_remaining"] == 0
    assert fr.list_all(status="running") == []
    # 4) 计划事件带回派单三要素（对话面板"计划了什么"的数据源）
    from secpt.core.job.resume import jobs_dir

    evs = [
        json.loads(line)
        for line in (jobs_dir() / base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    plan = next(e for e in evs if e["type"] == "plan_event")
    planned = {t["title"]: t for t in plan["payload"]["planned"]}
    assert planned["侦察指纹"]["max_wall_s"] == 120
    assert "不要打 payload" in planned["侦察指纹"]["notes"]
    assert planned["确认入口可达"]["self"] is True
