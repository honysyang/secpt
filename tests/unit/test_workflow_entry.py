"""工作流入口修复回归测试（2026-10-16 批次）。

覆盖：CLI 命令注册唯一性（doctor 去重）、dag.augment_objective 共享拼接、
单链 stalled → RESET 重建（设计 §3 接入 direct/单链路径）、anticheat 跨子 job
降级复核（frontier 聚合流防串味）、distill 从聚合 events.jsonl 重建轨迹、
SECPT_JUDGE_MODEL 终裁模型覆盖。
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# CLI 注册唯一性（doctor 重复注册回归）
# ---------------------------------------------------------------------------


def test_cli_commands_unique() -> None:
    from collections import Counter

    from secpt.cli.app import app

    names = [getattr(c, "name", None) or c.callback.__name__ for c in app.registered_commands]
    dups = [n for n, c in Counter(names).items() if c > 1]
    assert dups == [], f"CLI 命令重复注册: {dups}"


# ---------------------------------------------------------------------------
# augment_objective：solve 各模式与 dag 单链共用同一拼接口径
# ---------------------------------------------------------------------------


def test_augment_objective_appends_entries_and_artifacts() -> None:
    from secpt.core.orchestrator.dag import augment_objective

    out = augment_objective("目标", ["http://a.com", "10.0.0.1"], ["/abs/attachments/src"])
    assert "【目标入口】" in out and "http://a.com" in out
    assert "【附件源码" in out and "/abs/attachments/src" in out
    # 空输入零拼接（幂等，防重复 augment）
    assert augment_objective("目标", [], []) == "目标"


# ---------------------------------------------------------------------------
# 单链 stalled → RESET 重建（direct 路径接 RESET）
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, flag, steps, wall_s, status, messages=None):
        self.flag = flag
        self.steps = steps
        self.wall_s = wall_s
        self.status = status
        self.messages = messages or []
        self.job_id = ""


def _run_single(spec, fake_run_engine, monkeypatch, tmp_path):
    import asyncio as _a

    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    import secpt.cli.engine_runner as er

    orig = er.run_engine
    er.run_engine = fake_run_engine
    try:
        from secpt.core.orchestrator.dag import run_single_chain

        return _a.new_event_loop().run_until_complete(run_single_chain(spec, llm=object()))
    finally:
        er.run_engine = orig


class TestSingleChainReset:
    def test_stalled_with_ws_output_triggers_reset(self, tmp_path, monkeypatch):
        from secpt.core.job.models import TaskBudget, TaskSpec

        ws = tmp_path / "jobs" / "rt1" / "ws"
        ws.mkdir(parents=True)
        (ws / "probe.txt").write_text("v=1.0", encoding="utf-8")

        r1 = _FakeResult(
            flag=None,
            steps=80,
            wall_s=500.0,
            status="stalled",
            messages=[{"role": "assistant", "content": "试了很多路线没有进展"}],
        )
        r2 = _FakeResult(flag="FLAG{recovered}", steps=10, wall_s=60.0, status="success")
        calls: list[dict] = []

        def fake_run_engine(**kw):
            calls.append(kw)
            return r1 if len(calls) == 1 else r2

        spec = TaskSpec(job_id="rt1", objective="渗透目标", budget=TaskBudget(steps=100))
        summary = _run_single(spec, fake_run_engine, monkeypatch, tmp_path)

        assert len(calls) == 2
        assert calls[1]["job_id"] == "rt1-rs"  # RESET 换会话（新 job_id，非 -r2 续步）
        assert "【RESET 重建" in calls[1]["objective"]
        assert "probe.txt" in calls[1]["objective"]  # ws 产物清单已注入
        assert "试了很多路线" in calls[1]["objective"]  # handoff 摘要已注入
        assert summary["flags"] == ["FLAG{recovered}"]
        assert summary.get("reset") is True  # extras 展平到顶层（RunSummary.to_dict 口径）

    def test_stalled_without_ws_output_falls_back_to_r2(self, tmp_path, monkeypatch):
        """空转不重建（ws 无产出 → 原 r2 换路线重试路径）。"""
        from secpt.core.job.models import TaskBudget, TaskSpec

        (tmp_path / "jobs" / "rt2" / "ws").mkdir(parents=True)  # 空 ws

        r1 = _FakeResult(flag=None, steps=80, wall_s=500.0, status="stalled")
        r2 = _FakeResult(flag=None, steps=5, wall_s=30.0, status="failed")
        calls: list[dict] = []

        def fake_run_engine(**kw):
            calls.append(kw)
            return r1 if len(calls) == 1 else r2

        spec = TaskSpec(job_id="rt2", objective="渗透目标", budget=TaskBudget(steps=100))
        _run_single(spec, fake_run_engine, monkeypatch, tmp_path)

        assert len(calls) == 2
        assert calls[1]["job_id"] == "rt2-r2"  # 非 RESET 命名
        assert "【上一次尝试未成功】" in calls[1]["objective"]

    def test_reset_disabled_by_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_DIRECT_RESET", "0")
        from secpt.core.job.models import TaskBudget, TaskSpec

        ws = tmp_path / "jobs" / "rt3" / "ws"
        ws.mkdir(parents=True)
        (ws / "probe.txt").write_text("v=1.0", encoding="utf-8")

        r1 = _FakeResult(flag=None, steps=80, wall_s=500.0, status="stalled")
        r2 = _FakeResult(flag=None, steps=5, wall_s=30.0, status="failed")
        calls: list[dict] = []

        def fake_run_engine(**kw):
            calls.append(kw)
            return r1 if len(calls) == 1 else r2

        spec = TaskSpec(job_id="rt3", objective="渗透目标", budget=TaskBudget(steps=100))
        _run_single(spec, fake_run_engine, monkeypatch, tmp_path)

        assert calls[1]["job_id"] == "rt3-r2"  # SECPT_DIRECT_RESET=0 时走原 r2


# ---------------------------------------------------------------------------
# anticheat 跨子 job 降级（frontier 聚合流防串味）
# ---------------------------------------------------------------------------


class TestAntiCheatSubjob:
    def _evidence(self, read_sub: str, answer_sub: str):
        from secpt.evaluation.evidence import Evidence, ToolCall

        return Evidence(
            job_id="agg1",
            tool_calls=[
                ToolCall(
                    call_id="c1",
                    name="bash",
                    args=json.dumps({"command": "cat flag.txt"}),
                    status="ok",
                    preview="FLAG{x}",
                    sub=read_sub,
                )
            ],
            answer="FLAG{x}",
            answer_sub=answer_sub,
        )

    def test_same_subjob_still_hard_fail(self):
        from secpt.evaluation.anticheat import check

        rep = check(self._evidence(read_sub="i2", answer_sub="i2"), None)
        assert rep.local_answer_file_read is True
        assert rep.hard_fail is True

    def test_cross_subjob_downgraded_to_suspicious(self):
        from secpt.evaluation.anticheat import check

        # recon(i2) 直读到答案文件，exploit(i3) 产出 ANSWER——跨会话不硬判
        rep = check(self._evidence(read_sub="i2", answer_sub="i3"), None)
        assert rep.hard_fail is False
        assert rep.local_answer_file_read is False
        assert any("跨会话" in s for s in rep.suspicious)

    def test_single_chain_sub_empty_unchanged(self):
        from secpt.evaluation.anticheat import check

        rep = check(self._evidence(read_sub="", answer_sub=""), None)
        assert rep.hard_fail is True  # 单链（无 subjob 标记）维持硬判口径


# ---------------------------------------------------------------------------
# distill 从聚合 events.jsonl 重建轨迹（team 模式学习链）
# ---------------------------------------------------------------------------


def test_distill_loads_trajectory_from_events(tmp_path) -> None:
    from secpt.learning.distill import _load_job_text

    job_dir = tmp_path / "team1"
    job_dir.mkdir()
    events = [
        {
            "seq": 1,
            "type": "tool_call",
            "payload": {"tool": "bash", "args": '{"command": "nmap -sV target"}'},
        },
        {
            "seq": 2,
            "type": "tool_result",
            "payload": {"call_id": "c1", "status": "ok", "preview": "22/open ssh"},
        },
        {
            "seq": 3,
            "type": "assistant",
            "payload": {"content": "发现 ssh 开放", "subjob": "team1-i1"},
        },
    ]
    (job_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8"
    )
    _obj, traj = _load_job_text(job_dir)
    assert "nmap -sV target" in traj
    assert "22/open ssh" in traj
    assert "发现 ssh 开放" in traj


# ---------------------------------------------------------------------------
# SECPT_JUDGE_MODEL 覆盖终裁模型
# ---------------------------------------------------------------------------


def test_judge_model_env_override(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("SECPT_JUDGE_MODEL", "crossvendor/model-x")
    captured: list[str] = []

    class _CapturingJudge:
        def __init__(self, llm, model="deepseek-chat"):
            captured.append(model)

        async def judge(self, rubric, evidence, anticheat):
            from secpt.evaluation.judge import JudgeUnavailable

            raise JudgeUnavailable("capture")

    monkeypatch.setattr("secpt.evaluation.verdict.LLMJudge", _CapturingJudge)

    job_dir = tmp_path / "jm1"
    (job_dir / "ws").mkdir(parents=True)
    (job_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "type": "assistant",
                "payload": {"content": "ANSWER: FLAG{z}", "tool_calls": []},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    from secpt.evaluation.verdict import evaluate_job

    v = asyncio.new_event_loop().run_until_complete(
        evaluate_job("jm1", jobs_dir=tmp_path, llm=object(), model="deepseek-chat")
    )
    assert v.method == "heuristic"  # JudgeUnavailable → 兜底
    assert captured == ["crossvendor/model-x"]  # env 覆盖了调用方传入模型
