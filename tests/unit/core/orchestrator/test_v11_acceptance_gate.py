"""v11 接战确认闸单测：受理轮只产《接战单》+ frontier 挂起等确认 + 确认后编成派单。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

from secpt.core.job.models import JobRecord, TaskSpec
from secpt.core.job.store import JobStore
from secpt.core.orchestrator.commander import commander_round
from secpt.core.orchestrator.frontier import Frontier, frontier_db_path, run_frontier
from secpt.core.orchestrator.goals import load_goal
from secpt.core.orchestrator.hypothesis import HypothesisTree, hypothesis_db_path


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _ScriptedLLM:
    def __init__(self, payloads):
        self._p = list(payloads)
        self.prompts: list[str] = []

    async def call(self, messages, tools, model):
        self.prompts.append(str(messages[0].get("content") or ""))
        body = json.dumps(self._p.pop(0) if self._p else {"tasks": []}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])


_ACCEPTANCE = {
    "goal": {
        "objective": "拿下 test.local 的 flag",
        "restatement": "用户要的是可复现的入侵路径，不是扫描报告",
        "success_criteria": ["拿到 FLAG{...}", "附复现路径与影响说明"],
        "constraints": ["仅限授权范围"],
        "asset_scope": ["http://test.local"],
        "attack_steps": ["侦察", "利用"],
    },
    "risk_notes": ["涉及外联验证，需要授权"],
    "tasks": [{"title": "不应被受理轮派发的任务", "goal": "g", "done_criteria": "d"}],
}


def _mk_fr_tree(base: str):
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    return fr, tree


def test_acceptance_round_parses_goal_and_ignores_decoy_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    fr, tree = _mk_fr_tree("acc-1")
    llm = _ScriptedLLM([_ACCEPTANCE])
    outcome = _run(
        commander_round(
            "客户原始需求",
            fr,
            tree,
            llm,
            acceptance=True,
            need_goal=True,
        )
    )
    assert outcome.goal is not None
    assert outcome.goal.restatement == "用户要的是可复现的入侵路径，不是扫描报告"
    assert outcome.goal.success_criteria == ["拿到 FLAG{...}", "附复现路径与影响说明"]
    # 受理轮即使模型多产了 tasks 也忽略——确认前不入池
    assert fr.total() == 0
    assert outcome.n_tasks == 0


def test_frontier_confirm_gate_blocks_until_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    base = "gate-1"
    fr, _tree = _mk_fr_tree(base)
    llm = _ScriptedLLM(
        [
            _ACCEPTANCE,
            {
                "decision": "continue",
                "hypotheses": [
                    {
                        "title": "入口假设",
                        "statement": "存在注入面",
                        "test": "验证回显",
                        "confidence": 0.5,
                    }
                ],
                "strategy": "## 当前阶段\n侦察\n## 主攻方向\n入口\n## 明确不做\n无\n## 转向日志\n- 初始",
                "tasks": [
                    {
                        "title": "web 侦察",
                        "goal": "识别技术栈",
                        "done_criteria": "有结论",
                        "priority": 5,
                    }
                ],
            },
        ]
    )
    JobStore().upsert_job(
        JobRecord(
            job_id=base,
            spec=TaskSpec(job_id=base, task_type="ctf", objective="x"),
            status="running",
        )
    )
    ev = threading.Event()
    holder: dict = {}

    def _drive():
        holder["summary"] = _run(
            run_frontier(
                "客户原始需求：拿下 test 站",
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
                confirm_event=ev,
            )
        )

    t = threading.Thread(target=_drive, daemon=True)
    t.start()

    # 等受理轮落盘并进入挂起态（轮询，限 15s）
    deadline = time.time() + 15
    rec = None
    while time.time() < deadline:
        rec = JobStore().get_job(base)
        g = load_goal(base)
        if rec is not None and rec.status == "awaiting_confirm" and g is not None and g.restatement:
            break
        time.sleep(0.2)
    assert rec is not None and rec.status == "awaiting_confirm"
    goal = load_goal(base)
    assert goal is not None and goal.restatement and len(goal.success_criteria) == 2
    # 挂起期间：无任务入池（编成未发生）
    assert fr.total() == 0
    assert t.is_alive()  # 编排仍挂起

    ev.set()
    t.join(timeout=30)
    assert not t.is_alive()
    # 确认后编成派单发生
    assert fr.total() >= 1
    rec2 = JobStore().get_job(base)
    assert rec2 is not None and rec2.status != "awaiting_confirm"
