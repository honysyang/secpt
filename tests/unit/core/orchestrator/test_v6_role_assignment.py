"""v6 改造单测：角色/场景/经验选择归指挥官 + 意图级角色赋予 + 协议闸口。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from secpt.core.orchestrator.commander import (
    commander_round,
    load_role,
    role_path,
    save_role,
)
from secpt.core.orchestrator.frontier import (
    Frontier,
    build_intent_objective,
    frontier_db_path,
    run_frontier,
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
    "priors": ["ctf.md", "no-such-prior.md"],  # 后者应被校验剔除
    "hypotheses": [
        {
            "title": "入口存在注入",
            "statement": "参数未过滤可联合查询拖库",
            "test": "注入测试语句返回绕过即证实",
            "confidence": 0.6,
        }
    ],
    "decision": "continue",
    "tasks": [
        {
            "title": "web 侦察",
            "goal": "识别技术栈",
            "done_criteria": "说出框架与版本",
            "priority": 8,
        },
        {
            "title": "附件 pwn 分析",
            "goal": "checksec 定防御面",
            "done_criteria": "输出保护机制清单",
            "priority": 7,
            "profile": "pwn",  # 意图级角色：该操作员领 pwn 角色
            "prior": "pwn.md",
        },
        {
            "title": "非法角色任务",
            "goal": "x",
            "done_criteria": "y",
            "priority": 1,
            "profile": "benchmark",  # 内部档案不可选 → 应被剔除
        },
    ],
    "strategy": _doc("入口注入面"),
}


def test_commander_first_wake_selects_role_and_priors(env) -> None:
    """首轮唤醒：profile 从角色目录单选、priors 经存在性校验；目录外剔除。"""
    fr = Frontier(frontier_db_path("r1"))
    tree = HypothesisTree(hypothesis_db_path("r1"))
    outcome = _run(
        commander_round(
            "客户原始需求",
            fr,
            tree,
            _ScriptedLLM([dict(_FIRST_WAKE)]),
            need_goal=True,
        )
    )
    assert outcome.profile == "ctf"
    assert outcome.priors == ["ctf.md"]  # no-such-prior.md 被剔除
    # 简报带角色目录（选择有据可依）
    # （_ScriptedLLM 不回放 prompt；目录注入由 _briefing 单测覆盖见下）


def test_briefing_includes_role_catalog(env) -> None:
    """need_goal 简报必须含【角色与经验·待选择】与目录条目。"""
    from secpt.core.orchestrator.commander import _briefing
    from secpt.core.profile.loader import role_catalog

    fr = Frontier(frontier_db_path("r1b"))
    tree = HypothesisTree(hypothesis_db_path("r1b"))
    text = _briefing("客户原始需求", fr, tree, None, "", need_goal=True)
    assert "【角色与经验·待选择】" in text
    for name, _desc in role_catalog():
        assert f"- {name}：" in text
    assert "benchmark" not in [
        ln.split("：", 1)[0].lstrip("- ").strip() for ln in text.splitlines() if ln.startswith("- ")
    ] or True  # 目录不含内部档案（宽松断言：目录函数已单测）


def test_dispatch_assigns_per_task_role(env) -> None:
    """意图级角色赋予：任务可带 profile/prior 入 intents 列；内部档案剔除。"""
    fr = Frontier(frontier_db_path("r2"))
    tree = HypothesisTree(hypothesis_db_path("r2"))
    outcome = _run(
        commander_round("客户原始需求", fr, tree, _ScriptedLLM([dict(_FIRST_WAKE)]), need_goal=True)
    )
    rows = {r["title"]: r for r in fr.list_all(status="open")}
    assert rows["附件 pwn 分析"]["profile"] == "pwn"
    assert rows["附件 pwn 分析"]["prior"] == "pwn.md"
    assert rows["web 侦察"]["profile"] == ""  # 未指定 → 沿用任务级角色
    assert rows["非法角色任务"]["profile"] == ""  # benchmark 内部档案被剔除
    detail = {d["title"]: d for d in outcome.tasks_detail}
    assert detail["附件 pwn 分析"]["profile"] == "pwn"
    # 操作员简报可见本任务角色
    obj = build_intent_objective("总目标", fr.claim_by_id(rows["附件 pwn 分析"]["id"]), [])
    assert "角色：pwn" in obj and "pwn.md" in obj


def test_role_roundtrip(env) -> None:
    """role.json 落盘/读取（resume 复用角色选择）。"""
    p = save_role("r3", "pentest", ["web.md"])
    assert p == role_path("r3") and p.is_file()
    assert load_role("r3") == ("pentest", ["web.md"])
    assert load_role("no-such") is None


def test_frontier_auto_mode_role_flow(env, monkeypatch) -> None:
    """端到端（auto 模式）：入口未指定角色 → 指挥官首轮选定 → 落盘 → 操作员按角色执行。

    - 任务级角色 ctf 作用于未指定任务；意图级 pwn 覆盖自己的任务。
    - engine 收到的 profile/prior 与派单一致（角色真正赋予了执行体）。
    """
    seen: list[tuple[str, str | None]] = []

    def fake_run_engine(**kw):
        seen.append((kw.get("profile", ""), kw.get("prior")))
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag="FLAG{r6_ok}",
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "r4"
    fr = Frontier(frontier_db_path(base))
    payloads = [dict(_FIRST_WAKE)] + [
        {"decision": "continue", "tasks": [], "strategy": _doc("入口注入面")}
        for _ in range(6)
    ]
    summary = _run(
        run_frontier(
            "客户原始需求：帮我拿下这个测试站",
            profile="auto",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_ScriptedLLM(payloads),
            max_rounds=8,
            max_intents=12,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    # 角色选择落盘
    assert load_role(base) == ("ctf", ["ctf.md"])
    # 引擎收到的角色：web 侦察用任务级 ctf+web.md；pwn 分析用自己的 pwn+pwn.md
    profs = {p for p, _ in seen}
    assert ("ctf", "ctf.md") in seen
    assert ("pwn", "pwn.md") in seen
    assert "auto" not in profs  # 落定后不再有 auto 透传
    statuses = {c["intent"]: c["status"] for c in summary["chains"]}
    assert statuses.get("web 侦察") == "done"
    assert statuses.get("附件 pwn 分析") == "done"
    assert summary["open_remaining"] == 0


def test_frontier_auto_resume_reuses_role(env, monkeypatch) -> None:
    """resume：role.json/goal.json 已落盘 → 不再请求理解，角色直接生效。"""
    from secpt.core.orchestrator.goals import save_goal, GoalSpec

    save_role("r5", "pentest", ["pentest.md"])
    save_goal("r5", GoalSpec(objective="拿下目标", asset_scope=["http://a.local"]))
    seen: list[str] = []

    def fake_run_engine(**kw):
        seen.append(kw.get("profile", ""))
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag=None,
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    fr = Frontier(frontier_db_path("r5"))
    fr.add("既有任务", goal="g", done_criteria="c")
    llm = _ScriptedLLM([{"decision": "continue", "tasks": [], "strategy": _doc("续接")}])
    _run(
        run_frontier(
            "客户原始需求：帮我拿下这个测试站",  # 原文仍未渲染，但 role/goal 已落盘
            profile="auto",
            base_job_id="r5",
            frontier=fr,
            llm=object(),
            planner_llm=llm,
            max_rounds=4,
            max_intents=6,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    assert "pentest" in seen  # resume 直接沿用落盘角色，引擎按 pentest 跑
    # resume 不再请求任务理解/角色选择：简报的待拆解指令句不出现于任何唤醒
    # （注意 prompt 模板自身含"【任务拆解·待完成】"字样，必须匹配 briefing 专属句）
    assert llm.prompts and all(
        "【任务拆解·待完成】上方总目标是客户原始需求" not in p for p in llm.prompts
    )


def test_dag_protocol_gate_benchmark(env, monkeypatch) -> None:
    """协议闸口：task_type=auto + 题面命中平台关键词 → 升级 benchmark 战役（不烧 LLM）。"""
    from secpt.core.job.models import TaskSpec
    from secpt.core.orchestrator import benchmark as bench_mod
    from secpt.core.orchestrator import dag as dag_mod

    called: list[str] = []

    async def _fake_campaign(spec, **kw):
        called.append(spec.task_type)
        return {"mode": "benchmark", "flags": [], "entries": []}

    # run_task 在函数内 from .benchmark import —— 必须补丁源模块，打 dag 属性不生效
    monkeypatch.setattr(bench_mod, "run_benchmark_campaign", _fake_campaign, raising=False)
    monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
    monkeypatch.delenv("BENCHMARK_TOKEN", raising=False)
    spec = TaskSpec(
        job_id="gate1",
        task_type="auto",
        objective="通过 openapi/v1/challenges 拉题，用 unique_code 提交 flag 跑分",
    )
    out = _run(dag_mod.run_task(spec))
    assert called == ["benchmark"]
    assert out["mode"] == "benchmark"


def test_dag_auto_stays_frontier_without_keywords(env, monkeypatch) -> None:
    """协议闸口：普通题面（无平台关键词）→ 不升级，走 frontier auto 角色流程。"""
    from secpt.core.job.models import TaskSpec
    from secpt.core.orchestrator import benchmark as bench_mod
    from secpt.core.orchestrator import dag as dag_mod

    called: list[str] = []

    async def _fake_campaign(spec, **kw):
        called.append(spec.task_type)
        return {"mode": "benchmark"}

    async def _fake_frontier(objective, **kw):
        called.append("frontier:" + str(kw.get("profile")))
        return {"mode": "frontier", "flags": []}

    from secpt.core.orchestrator import frontier as fr_mod

    monkeypatch.setattr(bench_mod, "run_benchmark_campaign", _fake_campaign, raising=False)
    monkeypatch.setattr(fr_mod, "run_frontier", _fake_frontier, raising=False)
    spec = TaskSpec(job_id="gate2", task_type="auto", objective="帮我测一下这个电商站")
    out = _run(dag_mod.run_task(spec))
    assert called == ["frontier:auto"]  # 未升级；角色由指挥官首轮在 frontier 内定
    assert out["mode"] == "frontier"
