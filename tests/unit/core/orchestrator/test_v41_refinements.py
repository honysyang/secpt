"""v4.1 改造单测：假设上下文注入 / 防御信号 / 能力缺口出口 / 派单去重 / 信念回滚 / manifest。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from secpt.core.orchestrator.commander import commander_round
from secpt.core.orchestrator.frontier import (
    Frontier,
    _carry_ws_artifacts,
    build_intent_objective,
    frontier_db_path,
    run_frontier,
)
from secpt.core.orchestrator.hypothesis import HypothesisTree, hypothesis_db_path
from secpt.core.orchestrator.watch import detect_defensive_signals


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
    monkeypatch.setenv("SECPT_ORCH_DELIVERY", "0")
    monkeypatch.setenv("SECPT_ORCH_LOGISTICS", "0")
    return tmp_path


def _intent(**kw):
    from secpt.core.orchestrator.frontier import Intent

    defaults = dict(
        id="i1",
        title="试弱口令",
        goal="登录 SSH",
        done_criteria="拿到 flag",
        priority=9,
        depth=0,
        status="open",
        attempts=1,
    )
    defaults.update(kw)
    return Intent(**defaults)


# ---------------------------------------------------------------------------
# 智能体层：假设上下文注入 + 证据锚定要求
# ---------------------------------------------------------------------------


def test_objective_injects_hypothesis_context(env) -> None:
    base = "ctx1"
    tree = HypothesisTree(hypothesis_db_path(base))
    hid = tree.add(
        "SSH 存在弱口令",
        statement="root/toor 可登录目标 SSH",
        test="ssh 登录成功即证实",
        confidence=0.7,
        evidence_refs=["job-a9"],
    )
    obj = build_intent_objective(
        "总目标",
        _intent(hypothesis=hid),
        [],
        base=base,
    )
    assert "你在验证的假设" in obj
    assert hid in obj and "root/toor 可登录目标 SSH" in obj and "ssh 登录成功即证实" in obj
    assert "job-a9" in obj  # 已有证据
    assert "证据锚定" in obj  # 结论必须带证据指针


def test_objective_injects_finished_logistics(env) -> None:
    recent = [
        {"status": "done", "title": "后勤：下载字典", "result": "已就位 rockyou.txt"},
        {"status": "exhausted", "title": "别的失败任务", "result": "x"},
    ]
    obj = build_intent_objective("总目标", _intent(), recent, base="b", allow_logistics=True)
    assert "后勤已完成" in obj and "rockyou.txt" in obj


# ---------------------------------------------------------------------------
# 作战地图：防御信号检测
# ---------------------------------------------------------------------------


def test_defensive_signals_requires_repetition() -> None:
    single = ["HTTP/1.1 403 Forbidden"]
    assert detect_defensive_signals(single) == []  # 单次 403 不算
    repeated = ["403 Forbidden", "403 Forbidden again", "403 Forbidden x3"]
    assert detect_defensive_signals(repeated) == ["403 forbidden"]
    # 不同特征各自要达到重复阈值才计；零散单次不算
    mixed = ["429 Too Many Requests", "rate limit exceeded", "blocked by WAF"]
    assert detect_defensive_signals(mixed) == []
    repeated_429 = [
        "429 Too Many Requests",
        "429 Too Many Requests",
        "429 Too Many Requests x2",
    ]
    assert "429 too many requests" in detect_defensive_signals(repeated_429)


def test_settle_records_capability_gap_and_defensive(env, monkeypatch) -> None:
    """能力缺口 → [capability_gap] 证据；反复 403 → [defensive] 证据。"""
    tool_outputs = [
        {"role": "tool", "content": "HTTP/1.1 403 Forbidden"},
        {"role": "tool", "content": "Error: 命令 'nessus' 不存在。插件需要的外部工具未安装"},
        {"role": "tool", "content": "403 Forbidden again"},
        {"role": "tool", "content": "GET /admin -> 403 Forbidden"},
    ]

    def fake_run_engine(**kw):
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag=None,
            steps=1,
            wall_s=0.01,
            status="error",
            messages=[{"role": "assistant", "content": "试试"}, *tool_outputs],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "v41-settle"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    hid = tree.add("H", statement="足够长的可证伪陈述内容甲", test="判定实验甲")
    fr.add("试一下", goal="g", done_criteria="c", priority=9, hypothesis=hid)
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
            stop_on_first_flag=False,
            enable_delivery=False,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    notes = [e["note"] for e in tree.undigested_evidence(limit=20)]
    assert any("[capability_gap]" in n and "nessus" in n for n in notes)
    assert any("[defensive]" in n and "403 forbidden" in n for n in notes)


# ---------------------------------------------------------------------------
# 协作/效率：Commander 派单去重
# ---------------------------------------------------------------------------


def test_commander_dedupes_same_title(env) -> None:
    base = "v41-dedup"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    payload = {
        "tasks": [
            {"title": "扫描  端口", "goal": "g", "done_criteria": "c"},
            {"title": "扫描端口", "goal": "g", "done_criteria": "c"},  # 归一化后同名
            {"title": "另一件事", "goal": "g", "done_criteria": "c"},
        ]
    }
    outcome = _run(commander_round("目标", fr, tree, _Scripted(payload), model="m", path=None))
    assert outcome.n_tasks == 2  # 同名去重
    assert fr.count_open(lane="main") == 2


class _Scripted:
    def __init__(self, payload):
        self._p = payload

    async def call(self, messages, tools, model):
        body = json.dumps(self._p, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])


# ---------------------------------------------------------------------------
# 编排：交付复验失败 → 信念回滚
# ---------------------------------------------------------------------------


def test_delivery_rollback_lowers_confirmed_confidence(env, monkeypatch) -> None:
    """复验尝试耗尽仍未通过 → confirmed 假设置信度下调 + [rollback] 证据。"""
    monkeypatch.setenv("SECPT_ORCH_DELIVERY", "1")
    calls = {"n": 0}

    def fake_run_engine(**kw):
        calls["n"] += 1
        # 原任务拿到 flag；复验任务（source=delivery）给不出同一 flag → 复验失败
        if "复验" in str(kw.get("objective") or ""):
            return SimpleNamespace(
                job_id=kw.get("job_id"),
                flag=None,
                steps=1,
                wall_s=0.01,
                status="success",
                messages=[],
            )
        return SimpleNamespace(
            job_id=kw.get("job_id"),
            flag="FLAG{rollback}",
            steps=1,
            wall_s=0.01,
            status="success",
            messages=[],
        )

    monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
    base = "v41-rollback"
    fr = Frontier(frontier_db_path(base))
    tree = HypothesisTree(hypothesis_db_path(base))
    hid = tree.add("弱口令假设", statement="SSH 存在弱口令 root/toor 登录面", test="登录成功即证实")
    tree.set_state(hid, "testing")
    tree.set_state(hid, "confirmed", evidence_ref="job-x")
    fr.add("试弱口令", goal="登录", done_criteria="拿到 flag", priority=9, hypothesis=hid)
    summary = _run(
        run_frontier(
            "目标",
            profile="ctf",
            base_job_id=base,
            frontier=fr,
            llm=object(),
            planner_llm=_EmptyPlanner(),
            max_rounds=5,
            max_intents=8,
            stop_on_first_flag=True,
            enable_delivery=True,
            enable_logistics=False,
            enable_commander=True,
        )
    )
    node = tree.get(hid)
    assert node.confidence < 0.5  # 0.5 * 0.6 = 0.3
    assert any("[rollback]" in e["note"] for e in tree.undigested_evidence(limit=20))
    assert summary["delivery"]["accepted"] is True  # 复验耗尽按未验证收口


# ---------------------------------------------------------------------------
# 数据：工作区搬运清单持久化
# ---------------------------------------------------------------------------


def test_carry_ws_artifacts_writes_manifest(env) -> None:
    from secpt.core.job.resume import jobs_dir

    old_ws = jobs_dir() / "old-j" / "ws"
    old_ws.mkdir(parents=True, exist_ok=True)
    (old_ws / "exploit.py").write_text("payload", encoding="utf-8")
    lines = _carry_ws_artifacts("old-j", "new-j")
    assert "exploit.py" in lines
    import json

    manifest = json.loads(
        (jobs_dir() / "new-j" / "ws" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["carried_from"] == "old-j"
    assert any("exploit.py" in f for f in manifest["files"])
