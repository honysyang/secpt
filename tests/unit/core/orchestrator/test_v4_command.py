"""v4 编排单测：假设树 / 值守层 / Commander（防漂移闸、信念结算、派单）/ 接线。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from secpt.core.orchestrator.commander import (
    commander_round,
    drift_gate,
    load_strategy,
    save_strategy,
    strategy_path,
)
from secpt.core.orchestrator.frontier import Frontier, frontier_db_path, run_frontier
from secpt.core.orchestrator.hypothesis import (
    HypothesisError,
    HypothesisTree,
    hypothesis_db_path,
)
from secpt.core.orchestrator.watch import (
    BUDGET_EXHAUSTED,
    NEGATIVE,
    STUCK,
    SUCCESS,
    TOOL_FAILURE,
    Watch,
    classify_failure,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _EmptyPlanner:
    async def call(self, messages, tools, model):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# 假设树
# ---------------------------------------------------------------------------


def test_hypothesis_state_machine(env) -> None:
    tree = HypothesisTree(hypothesis_db_path("ht1"))
    hid = tree.add(
        "SAML 接口存在注入",
        statement="构造 ' 或 1=1 会在 /sso 响应中出现 SQL 错误特征",
        test="发送单引号探针，响应含 SQL 异常即证实",
    )
    node = tree.get(hid)
    assert node.state == "proposed" and node.confidence == 0.5
    # 非法迁移：proposed → confirmed 被拒
    with pytest.raises(HypothesisError):
        tree.set_state(hid, "confirmed")
    tree.set_state(hid, "testing")
    # confirmed 必须附证据指针
    with pytest.raises(HypothesisError):
        tree.set_state(hid, "confirmed")
    assert tree.set_state(hid, "confirmed", evidence_ref="job-abc") is True
    # 终态不可再迁移
    with pytest.raises(HypothesisError):
        tree.set_state(hid, "dead")


def test_hypothesis_requires_falsifiable_content(env) -> None:
    tree = HypothesisTree(hypothesis_db_path("ht2"))
    with pytest.raises(HypothesisError):
        tree.add("太短", statement="探测", test="看看")
    with pytest.raises(HypothesisError):
        tree.add("缺判定实验", statement="这是一个足够长的可证伪陈述", test="")


def test_hypothesis_refine_marks_parent(env) -> None:
    tree = HypothesisTree(hypothesis_db_path("ht3"))
    parent = tree.add("父假设", statement="目标入口存在认证绕过面", test="未授权访问 /admin")
    child = tree.add(
        "子假设：JWT none 攻击",
        statement="JWT 接受 alg=none",
        test="伪造 none token 调 /admin 返回 200",
        parent_id=parent,
    )
    assert tree.get(parent).state == "refined"
    assert tree.get(child).parent_id == parent
    assert tree.get(child).state == "proposed"
    # refined 不可再有子节点
    with pytest.raises(HypothesisError):
        tree.add("孙", statement="又一个足够长的可证伪陈述", test="判定实验", parent_id=parent)


def test_hypothesis_evidence_events(env) -> None:
    tree = HypothesisTree(hypothesis_db_path("ht4"))
    hid = tree.add("H", statement="足够长的可证伪陈述内容", test="判定实验")
    tree.record_evidence_event(hid, "job-1", "负结果")
    ev = tree.undigested_evidence()
    assert len(ev) == 1 and ev[0]["hid"] == hid
    tree.mark_consumed([ev[0]["id"]])
    assert tree.undigested_evidence() == []


def test_hypothesis_open_high_value_and_counts(env) -> None:
    tree = HypothesisTree(hypothesis_db_path("ht5"))
    a = tree.add("A", statement="足够长的可证伪陈述 A 内容", test="判定实验", confidence=0.8)
    b = tree.add("B", statement="足够长的可证伪陈述 B 内容", test="判定实验", confidence=0.2)
    tree.set_state(b, "dead", evidence_ref="budget")
    assert [h.id for h in tree.open_high_value()] == [a]
    assert tree.counts() == {"proposed": 1, "dead": 1}


# ---------------------------------------------------------------------------
# 值守层
# ---------------------------------------------------------------------------


def test_classify_failure_types() -> None:
    assert classify_failure("success") == SUCCESS
    assert classify_failure("done") == SUCCESS
    assert classify_failure("stalled", "无进展") == STUCK
    assert classify_failure("step_exceeded") == BUDGET_EXHAUSTED
    assert classify_failure("error", "启动失败") == TOOL_FAILURE
    assert classify_failure("error", "命令 'nessus' 不存在") != TOOL_FAILURE  # 能力缺口提示词命中
    # 正常结束但未达成 → negative（实验成功执行、结果为负，是信息不是故障）
    assert classify_failure("exhausted", "复核后仍否定") == NEGATIVE


def test_watch_budget_milestones_fire_once() -> None:
    w = Watch(max_intents=10)
    s1 = w.budget_signals(elapsed_s=0, total_wall_s=0, used_intents=4)
    assert [s.kind for s in s1] == ["budget_milestone"] and "40%" in s1[0].detail
    s2 = w.budget_signals(elapsed_s=0, total_wall_s=0, used_intents=4)
    assert s2 == []  # 未过新阈值，不重复
    s3 = w.budget_signals(elapsed_s=0, total_wall_s=0, used_intents=5)
    assert [s.kind for s in s3] == ["budget_milestone"]  # 50%
    # 墙钟口径：过 3/4 立即报
    w2 = Watch(max_intents=0)
    s4 = w2.budget_signals(elapsed_s=80, total_wall_s=100, used_intents=0)
    assert s4 and s4[0].kind == "budget_milestone"


def test_watch_consolidation_and_stall() -> None:
    w = Watch(consolidate_every=3, stall_rounds=2)
    assert w.consolidation_signal() is None
    assert w.consolidation_signal() is None
    sig = w.consolidation_signal()
    assert sig is not None and sig.kind == "consolidation"
    # 停滞：连续两次无进度才触发
    assert w.stall_signal(done_intents=0, tree_transitions=0) is None
    fired = w.stall_signal(done_intents=0, tree_transitions=0)
    assert fired is not None and fired.kind == "stall"
    # 进度出现 → 重置
    assert w.stall_signal(done_intents=1, tree_transitions=0) is None


# ---------------------------------------------------------------------------
# Commander：防漂移闸 / 唤醒
# ---------------------------------------------------------------------------


def _doc(direction: str, log: str = "- 2026-01-01 初始") -> str:
    return f"## 当前阶段\n侦察\n\n## 主攻方向\n{direction}\n\n## 明确不做\n无\n\n## 转向日志\n{log}"


def test_drift_gate() -> None:
    old = _doc("主攻 SAML 注入面")
    # 小改：通过
    ok, _ = drift_gate(old, _doc("主攻 SAML 注入面与 XSS"))
    assert ok
    # 大改无论证：拒收
    ok, why = drift_gate(old, _doc("主攻内网横向移动"))
    assert not ok and "转向日志" in why
    # 大改 + 转向日志新论证：通过
    ok, _ = drift_gate(
        old,
        _doc("主攻内网横向移动", "- 2026-06-01 证据 E12 证伪注入假设，转横向"),
        pivot_justified=True,
    )
    assert ok


class _CommanderLLM:
    """按脚本返回 Commander JSON。"""

    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = list(payloads)

    async def call(self, messages, tools, model):
        body = json.dumps(
            self._payloads.pop(0) if self._payloads else {"tasks": []}, ensure_ascii=False
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])


def test_commander_beliefs_tasks_and_strategy(env) -> None:
    base = "cmd1"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    hid = tree.add("存在弱口令", statement="SSH 存在弱口令 root/toor", test="hydra 爆破成功即证实")
    tree.record_evidence_event(hid, "job-9", "hydra 命中 root/toor")
    path = strategy_path(base)

    payload = {
        "beliefs": [{"id": hid, "state": "testing", "confidence": 0.9, "evidence": "job-9"}],
        "hypotheses": [],
        "decision": "continue",
        "justification": "",
        "tasks": [
            {
                "title": "用拿到的凭据登录 SSH",
                "goal": "验证 root/toor 可登录",
                "done_criteria": "拿到 shell 或明确失败",
                "priority": 8,
                "lane": "main",
                "hypothesis": hid,
            },
            {
                "title": "下载工具",
                "goal": "后台下载",  # 含"下载"→ triage 归后勤
                "done_criteria": "下载完成",
                "lane": "logistics",
            },
            {"title": "", "goal": "空标题应被丢弃"},
        ],
        "strategy": _doc("主攻弱口令与凭据复用"),
    }
    outcome = _run(
        commander_round(
            "目标",
            fr,
            tree,
            _CommanderLLM([payload]),
            model="m",
            signal={"kind": "stall"},
            path=path,
        )
    )
    assert outcome.n_tasks == 2
    assert outcome.beliefs_applied == 1
    assert tree.get(hid).state == "testing"
    assert tree.undigested_evidence() == []  # 证据被信念结算消费
    rows = fr.list_all()
    by_title = {r["title"]: r for r in rows}
    assert by_title["用拿到的凭据登录 SSH"]["lane"] == "main"
    assert by_title["用拿到的凭据登录 SSH"]["hypothesis"] == hid
    assert by_title["下载工具"]["lane"] == "logistics"
    assert load_strategy(path)  # 战略文档落盘
    assert outcome.decision == "continue"


def test_commander_bare_array_compat_and_drift_reject(env) -> None:
    base = "cmd2"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    path = strategy_path(base)
    save_strategy(path, _doc("主攻 SAML 注入面"))
    # 裸数组：v3 兼容路径
    outcome = _run(
        commander_round(
            "目标",
            fr,
            tree,
            _CommanderLLM(
                [{"tasks": []}][:0] or [[{"title": "任务A", "goal": "g", "done_criteria": "c"}]]
            ),
            model="m",
            path=path,
        )
    )
    assert outcome.n_tasks == 1 and fr.total() == 1
    # 对象形但主攻方向大改且无论证 → 拒收文档，任务照常
    payload = {
        "decision": "pivot",
        "justification": "",
        "tasks": [{"title": "任务B", "goal": "g", "done_criteria": "c"}],
        "strategy": _doc("主攻内网横向移动"),
    }
    outcome2 = _run(
        commander_round("目标", fr, tree, _CommanderLLM([payload]), model="m", path=path)
    )
    assert outcome2.drift_rejected is True
    assert "主攻 SAML 注入面" in load_strategy(path)  # 旧文档保留
    assert fr.count_open(lane="main") == 2  # 任务照常入池


def test_commander_deliver_decision_surfaces(env) -> None:
    base = "cmd3"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    payload = {"decision": "deliver", "justification": "假设全部证实", "tasks": []}
    outcome = _run(
        commander_round(
            "目标", fr, tree, _CommanderLLM([payload]), model="m", path=strategy_path(base)
        )
    )
    assert outcome.decision == "deliver"


# ---------------------------------------------------------------------------
# 集成：commander 驱动的 run_frontier（信念收敛防误收口 + 挂靠假设确认）
# ---------------------------------------------------------------------------


def test_frontier_commander_keeps_run_alive_while_hypotheses_open(env, monkeypatch) -> None:
    """池空但假设树仍有活动节点 → 值守层唤醒 Commander 继续派单，不因 idle 收口。"""
    calls = {"n": 0}

    def fake_run_engine(**kw):
        calls["n"] += 1
        return SimpleNamespace(
            job_id=kw.get("job_id"), flag=None, steps=1, wall_s=0.01, status="success", messages=[]
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "v4-alive"
    fr = Frontier(frontier_db_path(base))
    fr.add("首任务", goal="推进", done_criteria="任意结论", priority=9)
    # Commander 第一轮：提假设+派任务；第二轮起：空任务（但假设仍活动）
    payloads = [
        {
            "decision": "continue",
            "hypotheses": [
                {
                    "title": "H1",
                    "statement": "足够长的可证伪陈述内容一",
                    "test": "判定实验一",
                    "confidence": 0.8,
                }
            ],
            "tasks": [{"title": "任务一", "goal": "g", "done_criteria": "c"}],
            "strategy": _doc("主攻方向一"),
        },
        {"decision": "continue", "tasks": [], "strategy": _doc("主攻方向一")},
        {"decision": "continue", "tasks": [], "strategy": _doc("主攻方向一")},
        {"decision": "deliver", "justification": "预算考虑", "tasks": []},
    ]
    summary = _run(
        run_frontier(
            "目标",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_ScriptedLLM(payloads),
            max_rounds=8,
            max_intents=10,
            stop_on_first_flag=True,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    assert calls["n"] >= 1  # 任务被实际执行
    assert any(c["intent"] == "任务一" for c in summary["chains"])
    # 池空后值守层继续唤醒 Commander（信念收敛检查），deliver 请求被记录
    assert summary["commander_wakes"] >= 3
    assert summary["commander_deliver_requested"] is True
    assert summary["hypothesis_counts"].get("proposed") == 1  # H1 仍未决（收口因预算封顶）
    # v4.2 轨迹层：规划/意图生命周期事件直写父流（对话面板回放数据源）
    import json as _json

    from secpt.core.job.resume import jobs_dir

    evs = [
        _json.loads(line)
        for line in (jobs_dir() / base / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    plan_evs = [e for e in evs if e["type"] == "plan_event"]
    intent_evs = [e for e in evs if e["type"] == "intent_event"]
    assert plan_evs and all("decision" in e["payload"] for e in plan_evs)
    assert any(e["payload"]["status"] == "claimed" for e in intent_evs)
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # 父流 seq 单调不重


class _ScriptedLLM:
    def __init__(self, payloads):
        self._p = list(payloads)

    async def call(self, messages, tools, model):
        body = json.dumps(self._p.pop(0) if self._p else {"tasks": []}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])


def test_frontier_settle_updates_hypothesis_tree(env, monkeypatch) -> None:
    """挂靠假设的任务 done → 确定性信念结算（proposed→testing→confirmed）。"""

    def fake_run_engine(**kw):
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag="FLAG{v4win}",
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "v4-settle"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    hid = tree.add("弱口令", statement="SSH 存在弱口令 root/toor 登录面", test="登录成功即证实")
    fr.add("试弱口令", goal="登录", done_criteria="拿到 flag", priority=9, hypothesis=hid)
    _run(
        run_frontier(
            "目标",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_EmptyPlanner(),
            max_rounds=3,
            max_intents=5,
            stop_on_first_flag=True,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    node = tree.get(hid)
    assert node.state == "confirmed"
    assert any("job" in r for r in node.evidence_refs) or node.evidence_refs


def test_frontier_prunes_dead_hypothesis_tasks(env, monkeypatch) -> None:
    """内务修剪：挂靠假设 dead/refuted 的未开始任务被取消。"""

    def fake_run_engine(**kw):
        return SimpleNamespace(
            job_id=kw.get("job_id"), flag=None, steps=1, wall_s=0.01, status="success", messages=[]
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "v4-prune"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    hid = tree.add("死假设", statement="足够长的可证伪陈述内容二", test="判定实验二")
    tree.set_state(hid, "dead", evidence_ref="budget")
    fr.add("该取消的任务", goal="g", done_criteria="c", hypothesis=hid)
    _run(
        run_frontier(
            "目标",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_EmptyPlanner(),
            max_rounds=4,
            max_intents=10,
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    row = fr.list_all()[0]
    assert row["status"] == "exhausted" and "dead" in (row["fail_reason"] or "")


def test_delivery_drains_running_logistics(env, monkeypatch) -> None:
    """收敛排空：主路径完成后，在途后勤结算前交付门等待；后勤未开始的被取消。"""

    def fake_run_engine(**kw):
        return SimpleNamespace(
            job_id=kw.get("job_id"), flag=None, steps=1, wall_s=0.01, status="success", messages=[]
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "v4-drain"
    fr = Frontier(frontier_db_path(base))
    fr.add("主任务", goal="推进", done_criteria="c", priority=9)
    fr.add("未开始后勤", goal="后台", lane="logistics")
    summary = _run(
        run_frontier(
            "目标",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_EmptyPlanner(),
            max_rounds=4,
            max_intents=10,
            stop_on_first_flag=False,
            enable_delivery=True,
            enable_logistics=True,
            enable_commander=False,
        )
    )
    rows = {r["title"]: r for r in fr.list_all()}
    assert rows["未开始后勤"]["status"] == "exhausted"  # 未开始的后勤被取消
    assert "delivery" in summary  # 交付门收口
