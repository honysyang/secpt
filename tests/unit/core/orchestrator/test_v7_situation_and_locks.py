"""v7 改造单测：角色场景目录 + 经验目录 + 多信号一次交付 + 态势简报 + 父流跨写入方锁。"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from secpt.core.job.event import EventSink
from secpt.core.orchestrator.commander import _briefing
from secpt.core.orchestrator.frontier import (
    Frontier,
    _parent_event,
    frontier_db_path,
    run_frontier,
)
from secpt.core.orchestrator.hypothesis import HypothesisTree, hypothesis_db_path
from secpt.core.orchestrator.watch import WakeSignal
from secpt.core.profile.loader import (
    _INTERNAL_PROFILES,
    prior_catalog,
    role_catalog,
    valid_prior,
    valid_role,
)


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
        self.prompts: list[str] = []

    async def call(self, messages, tools, model):
        self.calls += 1
        self.prompts.append(str(messages[0].get("content") or ""))
        body = json.dumps(self._p.pop(0) if self._p else {"tasks": []}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])


def _doc(direction: str) -> str:
    return (
        "## 当前阶段\n侦察\n## 主攻方向\n"
        + direction
        + "\n## 明确不做\n无\n## 转向日志\n- 初始"
    )


_FIRST_WAKE = {
    "goal": {
        "objective": "拿下 test 靶机的 flag",
        "constraints": ["仅限授权范围"],
        "asset_scope": ["http://test.local"],
        "attack_steps": ["侦察", "利用"],
    },
    "profile": "ctf",
    "priors": ["ctf.md"],
    "decision": "continue",
    "tasks": [
        {
            "title": "web 侦察",
            "goal": "识别技术栈",
            "done_criteria": "有结论",
            "priority": 5,
        }
    ],
    "strategy": _doc("入口面"),
}


# ── 角色目录：场景指引 + web 档案 ──────────────────────────────────────────


def test_role_catalog_carries_scenario(env) -> None:
    """role_catalog 三元组含场景指引；内部档案不入目录；web 新档案可选。"""
    cat = role_catalog()
    assert cat, "目录不应为空"
    for name, desc, scenario in cat:
        assert name and desc and scenario, f"角色 {name} 缺说明或场景指引"
        assert name not in _INTERNAL_PROFILES
    names = {n for n, _, _ in cat}
    assert "web" in names
    assert "benchmark" not in names
    # 场景指引注入"何时选我"——指挥官选择的依据，不是空泛标签
    web_sc = next(sc for n, _, sc in cat if n == "web")
    assert "Web" in web_sc or "web" in web_sc
    assert valid_role("web") and not valid_role("benchmark")


def test_prior_catalog_entries(env) -> None:
    """经验目录：真实文件名 + 首行标题/适用面，供指挥官对照选择。"""
    priors = prior_catalog()
    assert priors, "经验目录不应为空"
    for fn, title in priors:
        assert fn.endswith(".md") and title, f"经验 {fn} 缺标题"
    names = {fn for fn, _ in priors}
    assert "ctf.md" in names  # 常用经验在册
    for fn, _ in priors:
        assert valid_prior(fn)  # 目录与校验同源（都是真实文件）


# ── 简报：多信号 / 战果 / 在途态势 ─────────────────────────────────────────


def test_briefing_renders_multi_signal_and_situation(env) -> None:
    """值守层积压多条信号一次交付；简报同时呈现战果与在途任务。"""
    fr = Frontier(frontier_db_path("v7b"))
    tree = HypothesisTree(hypothesis_db_path("v7b"))
    sigs = [
        {"kind": "budget_milestone", "detail": "预算已消耗约 50%", "priority": 7},
        {"kind": "stall", "detail": "连续两轮无信念增量", "priority": 6},
    ]
    text = _briefing(
        "客户原始需求",
        fr,
        tree,
        sigs,
        "",
        flags=["FLAG{one}", "FLAG{two}"],
        running=[{"title": "web 侦察", "elapsed_s": 12, "lane": "main"}],
    )
    assert "预算已消耗约 50%" in text and "连续两轮无信念增量" in text
    assert "【当前战果" in text and "FLAG{one}" in text
    assert "【在途任务" in text and "web 侦察" in text


# ── 端到端：双信号同轮到达 → 简报双议题 + plan_event wake=multi ────────────


def test_e2e_multi_signal_single_wake(env, monkeypatch) -> None:
    """值守层同轮攒下两条信号：一次唤醒全交付（防信号饿死），wake 负载标记 multi。"""
    from secpt.core.job.resume import jobs_dir
    from secpt.core.orchestrator.commander import commander_round as real_round

    captured: list[object] = []

    async def spy_round(objective, fr, tree, llm, **kw):
        captured.append(kw.get("signal"))
        return await real_round(objective, fr, tree, llm, **kw)

    monkeypatch.setattr(
        "secpt.core.orchestrator.commander.commander_round", spy_round
    )

    def fake_budget_signals(self, *, elapsed_s, total_wall_s, used_intents):
        return [
            WakeSignal(kind="budget_milestone", detail="预算信号甲", priority=7),
            WakeSignal(kind="consolidation", detail="内务信号乙", priority=4),
        ]

    monkeypatch.setattr(
        "secpt.core.orchestrator.watch.Watch.budget_signals", fake_budget_signals
    )

    def fake_run_engine(**kw):
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag="FLAG{v7_ok}",
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)

    base = "v7e2e"
    fr = Frontier(frontier_db_path(base))
    payloads = [dict(_FIRST_WAKE)] + [
        {"decision": "continue", "tasks": [], "strategy": _doc("维持")}
        for _ in range(6)
    ]
    llm = _ScriptedLLM(payloads)
    _run(
        run_frontier(
            "客户原始需求：拿下测试站",
            profile="auto",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=llm,
            max_rounds=8,
            max_intents=12,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    # 至少一次唤醒拿到的是信号列表且两条都在
    lists = [s for s in captured if isinstance(s, list)]
    assert lists, f"没有任何唤醒收到信号列表：{captured!r}"
    first = lists[0]
    assert len(first) >= 2
    details = json.dumps(first, ensure_ascii=False)
    assert "预算信号甲" in details and "内务信号乙" in details
    # 同轮简报两条议题均呈现
    brief = "\n".join(llm.prompts)
    assert "预算信号甲" in brief and "内务信号乙" in brief
    # plan_event wake 负载：多信号 → kind=multi
    parent_events = jobs_dir() / base / "events.jsonl"
    wakes = []
    if parent_events.is_file():
        for ln in parent_events.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "plan_event":
                wakes.append((ev.get("payload") or {}).get("wake"))
    assert any(w and w.get("kind") == "multi" for w in wakes), f"wakes={wakes!r}"


# ── 健壮：父事件流跨写入方并发不撞 seq ─────────────────────────────────────


def test_parent_stream_concurrent_writers_unique_seq(env, monkeypatch) -> None:
    """EventSink 与 _parent_event 并发写同一父流：seq 全局唯一且连续。"""
    from secpt.core.job.resume import jobs_dir

    base = "v7lock"
    sink = EventSink(base, jobs_dir())
    n_parent, n_sink = 40, 40
    threads = [
        threading.Thread(target=_parent_event, args=(base, "intent_event", {"i": i}))
        for i in range(n_parent)
    ] + [
        threading.Thread(target=sink.write, args=("tool_call", {"i": i}))
        for i in range(n_sink)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    path = jobs_dir() / base / "events.jsonl"
    seqs = [json.loads(ln)["seq"] for ln in path.read_text(encoding="utf-8").splitlines()]
    assert len(seqs) == n_parent + n_sink
    assert len(set(seqs)) == len(seqs), f"seq 重号：{sorted(seqs)}"
    assert sorted(seqs) == list(range(1, len(seqs) + 1)), "seq 不连续"
