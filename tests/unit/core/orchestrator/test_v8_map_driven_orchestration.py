"""v8 改造单测：地图质变当轮唤醒 + 收敛裁决归指挥官 + 操作员目标态势注入。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from secpt.core.orchestrator.commander import strategy_path
from secpt.core.orchestrator.frontier import (
    Frontier,
    _strategy_focus,
    build_intent_objective,
    frontier_db_path,
    run_frontier,
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
    return "## 当前阶段\n侦察\n## 主攻方向\n" + direction + "\n## 明确不做\n无\n## 转向日志\n- 初始"


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
            "goal": "识别技术栈并拿到入口",
            "done_criteria": "有结论",
            "priority": 5,
        }
    ],
    "strategy": _doc("入口注入面"),
}


def _engine_flag(**kw):
    return SimpleNamespace(
        job_id=kw.get("job_id"),
        flag="FLAG{v8_ok}",
        steps=1,
        wall_s=0.01,
        status="success",
        messages=[],
    )


# ── 质变检测：flag/confirmed 变化当轮插队唤醒 ────────────────────────────────


def test_material_change_wakes_commander_same_round(env, monkeypatch) -> None:
    """操作员拿下 flag（地图质变）→ 当轮 指挥官 收到 material_change 信号。"""
    from secpt.core.orchestrator.commander import commander_round as real_round

    captured: list[object] = []

    async def spy_round(objective, fr, tree, llm, **kw):
        captured.append(kw.get("signal"))
        return await real_round(objective, fr, tree, llm, **kw)

    monkeypatch.setattr("secpt.core.orchestrator.commander.commander_round", spy_round)
    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", _engine_flag)

    base = "v8mat"
    fr = Frontier(frontier_db_path(base))
    payloads = [dict(_FIRST_WAKE)] + [
        {"decision": "continue", "tasks": [], "strategy": _doc("维持")} for _ in range(8)
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
            max_rounds=10,
            max_intents=12,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    # 至少一次唤醒携带 material_change 信号（列表形态，v7 多信号交付）
    mats = []
    for sig in captured:
        items = sig if isinstance(sig, list) else [sig]
        mats += [x for x in items if x and x.get("kind") == "material_change"]
    assert mats, f"未见 material_change 信号：{captured!r}"
    assert "真实 flag 0→1" in json.dumps(mats, ensure_ascii=False)
    # 简报呈现质变议题
    assert any("地图质变" in p for p in llm.prompts)


# ── 收敛裁决归 指挥官：SNT 递 convergence，指挥官 可交付可补产；不交付有回退 ──────


def test_convergence_signal_goes_to_commander(env, monkeypatch) -> None:
    """子目标全结算 → 指挥官 收到 convergence 信号并裁决 deliver → 直接收口。"""
    from secpt.core.orchestrator.commander import commander_round as real_round

    captured: list[object] = []

    async def spy_round(objective, fr, tree, llm, **kw):
        captured.append(kw.get("signal"))
        return await real_round(objective, fr, tree, llm, **kw)

    monkeypatch.setattr("secpt.core.orchestrator.commander.commander_round", spy_round)
    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", _engine_flag)

    base = "v8conv"
    fr = Frontier(frontier_db_path(base))
    # 首轮理解+派单；质变小试不误交付；空响应两次把 idle 顶满；
    # 收敛信号到达时交付
    payloads = [
        dict(_FIRST_WAKE),
        {"decision": "continue", "tasks": [], "strategy": _doc("维持")},
        {"tasks": []},
        {"decision": "deliver", "tasks": [], "strategy": _doc("已达成")},
    ] + [{"decision": "deliver", "tasks": [], "strategy": _doc("已达成")} for _ in range(6)]
    llm = _ScriptedLLM(payloads)
    summary = _run(
        run_frontier(
            "客户原始需求：拿下测试站",
            profile="auto",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=llm,
            max_rounds=12,
            max_intents=12,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    convs = []
    for sig in captured:
        items = sig if isinstance(sig, list) else [sig]
        convs += [x for x in items if x and x.get("kind") == "convergence"]
    assert convs, f"收敛未递交 指挥官：{captured!r}"
    # 指挥官 交付裁决被尊重：正常收口而非 max_rounds 耗尽
    assert summary["open_remaining"] == 0
    assert summary["chains"], "任务应正常结算"
    # 收敛议题呈现在简报中
    assert any("convergence" in p or "已结算" in p for p in llm.prompts)


def test_convergence_backstop_terminates_when_commander_idle(env, monkeypatch) -> None:
    """回退保险：指挥官 连续不交付（空响应）→ SNT 兜底收口，绝不悬挂。"""
    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", _engine_flag)
    base = "v8back"
    fr = Frontier(frontier_db_path(base))
    # 首轮派单；之后全部空响应（decision 默认 continue、无任务）
    payloads = [dict(_FIRST_WAKE)] + [{"tasks": []} for _ in range(8)]
    summary = _run(
        run_frontier(
            "客户原始需求：拿下测试站",
            profile="auto",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_ScriptedLLM(payloads),
            max_rounds=14,
            max_intents=12,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    assert summary["open_remaining"] == 0


# ── 操作员目标注入：主攻方向 + 已验证漏洞情报 ────────────────────────────────


def test_strategy_focus_extracted_and_injected(env) -> None:
    """战略文档 ## 主攻方向 摘录注入操作员目标编译。"""
    base = "v8focus"
    p = strategy_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_doc("侧翼 Redis 未授权访问"), encoding="utf-8")
    assert _strategy_focus(p) == "侧翼 Redis 未授权访问"
    assert _strategy_focus(None) == ""  # 无战略文档（指挥官未启用）不注入

    fr = Frontier(frontier_db_path(base))
    fr.add(
        title="redis 探测",
        goal="确认 6379 是否可未授权访问",
        done_criteria="拿到结论",
        priority=5,
    )
    intent = fr.claim_next()
    text = build_intent_objective("拿下目标", intent, [], base=base, focus=_strategy_focus(p))
    assert "【当前主攻方向" in text and "侧翼 Redis 未授权访问" in text


def test_confirmed_findings_shared_into_objective(env, monkeypatch) -> None:
    """已确认漏洞（跨资产情报）注入操作员目标：复用而非重挖。"""
    import os
    from pathlib import Path

    from secpt.findings.models import Finding
    from secpt.findings.store import FindingStore

    base = "v8find"
    store = FindingStore(Path(os.environ["SECPT_VAR"]) / "findings.sqlite")
    store.save(
        Finding(
            title="后台 /admin 存在未授权访问",
            severity="high",
            status="confirmed",
            target="http://test.local",
            reproduction="GET /admin 直接可入",
            job_id=f"{base}-sub1",
        )
    )
    fr = Frontier(frontier_db_path(base))
    fr.add(
        title="横向移动探测",
        goal="在同网段其他资产上复用入口模式",
        done_criteria="有结论",
        priority=5,
    )
    intent = fr.claim_next()
    text = build_intent_objective("拿下目标", intent, [], base=base)
    assert "【已验证漏洞·跨资产情报" in text
    assert "后台 /admin 存在未授权访问" in text
    # 其他 job 的 finding 不越界泄漏
    store.save(
        Finding(
            title="别的任务的洞",
            severity="low",
            status="confirmed",
            job_id="other-job-1",
        )
    )
    text2 = build_intent_objective("拿下目标", intent, [], base=base)
    assert "别的任务的洞" not in text2
