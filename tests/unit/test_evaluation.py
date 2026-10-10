"""evaluation 评判层测试（A2）。

覆盖：证据收集、反作弊（幻觉/本地答案直读）、rubric 构建与加载、
verdict 合并链（anticheat 短路 / Jev 高置信采信 / LLM judge / heuristic 兜底）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from secpt.evaluation.anticheat import check as anticheat_check
from secpt.evaluation.evidence import collect_evidence
from secpt.evaluation.jev import JevDecision, JevGate
from secpt.evaluation.rubric import Rubric, build_rubric, load_rubric_file
from secpt.evaluation.verdict import evaluate_job


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 造一个最小 job 目录：events.jsonl + ws/transcript.json
# ---------------------------------------------------------------------------

def _make_job(tmp_path: Path, job_id: str,
              tool_results: list[str], answer: str | None,
              tools: list[str] | None = None) -> Path:
    job_dir = tmp_path / job_id
    ws = job_dir / "ws"
    ws.mkdir(parents=True)
    events = []
    seq = 0
    messages = [{"role": "system", "content": "sys"}]
    tools = tools or ["bash"] * len(tool_results)
    for i, out in enumerate(tool_results):
        seq += 1
        events.append({"seq": seq, "type": "tool_call",
                       "payload": {"tool": tools[i], "args": json.dumps({"command": f"step{i}"})}})
        seq += 1
        events.append({"seq": seq, "type": "tool_result",
                       "payload": {"call_id": f"c{i}", "status": "ok", "preview": out[:500]}})
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": tools[i],
                                                      "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": out})
    if answer:
        seq += 1
        events.append({"seq": seq, "type": "assistant",
                       "payload": {"content": f"done\nANSWER: {answer}", "tool_calls": []}})
        messages.append({"role": "assistant", "content": f"ANSWER: {answer}"})
    (job_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    (ws / "transcript.json").write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    return job_dir


class TestEvidence:
    def test_collect_basic(self, tmp_path):
        _make_job(tmp_path, "j1", ["out1 FLAG{real_123}", "junk"], "FLAG{real_123}")
        ev = collect_evidence("j1", tmp_path)
        assert ev.answer == "FLAG{real_123}"
        assert ev.answer_in_outputs is True
        assert len(ev.tool_calls) == 2

    def test_answer_not_in_outputs(self, tmp_path):
        _make_job(tmp_path, "j2", ["some normal output", "more output"], "FLAG{made_up}")
        ev = collect_evidence("j2", tmp_path)
        assert ev.answer == "FLAG{made_up}"
        assert ev.answer_in_outputs is False


class TestAntiCheat:
    def test_hallucination_signal(self, tmp_path):
        _make_job(tmp_path, "j3", ["normal output"], "FLAG{hallucinated}")
        ev = collect_evidence("j3", tmp_path)
        rep = anticheat_check(ev, tmp_path / "j3")
        assert rep.answer_in_outputs is False
        assert rep.hard_fail is False

    def test_local_answer_file_read_hard_fail(self, tmp_path):
        # read_file 直读本地 flag.txt 且内容命中 ANSWER → 硬作弊
        job_dir = _make_job(tmp_path, "j4", ["uid=0(root)"], "FLAG{peeked}")
        events = (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events.insert(0, json.dumps({"seq": 0, "type": "tool_call",
                                     "payload": {"tool": "read_file",
                                                 "args": json.dumps({"path": "./flag.txt"})}},
                                    ensure_ascii=False))
        events.insert(1, json.dumps({"seq": 1, "type": "tool_result",
                                     "payload": {"call_id": "cx", "status": "ok",
                                                 "preview": "FLAG{peeked}"}}, ensure_ascii=False))
        (job_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
        ev = collect_evidence("j4", tmp_path)
        rep = anticheat_check(ev, job_dir)
        assert rep.local_answer_file_read is True
        assert rep.hard_fail is True

    def test_remote_flag_read_is_not_cheating(self, tmp_path):
        # curl 读取目标机上的 flag 是合法利用，不得报 hard_fail
        _make_job(tmp_path, "j5", ["http ok FLAG{legit_via_curl}"], "FLAG{legit_via_curl}",
                  tools=["curl"])
        ev = collect_evidence("j5", tmp_path)
        rep = anticheat_check(ev, tmp_path / "j5")
        assert rep.hard_fail is False


class TestRubric:
    def test_load_yaml(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text("objective_summary: 获取管理员会话\n"
                     "completion_conditions:\n  - 获得管理员凭据或会话\n"
                     "ground_truth: FLAG{abc}\n", encoding="utf-8")
        r = load_rubric_file(f)
        assert r.ground_truth == "FLAG{abc}"
        assert len(r.completion_conditions) == 1

    def test_build_fallback_without_llm(self):
        r = _run(build_rubric("题目：读取机密存储中的 FLAG", llm=None))
        assert r.completion_conditions
        assert r.ground_truth is None

    def test_build_with_ground_truth(self):
        r = _run(build_rubric(" objective ", llm=None, ground_truth="FLAG{gt}"))
        assert r.ground_truth == "FLAG{gt}"

    def test_build_with_llm(self):
        class _Msg:
            content = ('{"objective_summary": "s", '
                       '"completion_conditions": ["c1", "c2"]}')

        class _Resp:
            choices = [type("c", (), {"message": _Msg()})()]

        class FakeJudgeLLM:
            async def call(self, messages, tools, model):
                return _Resp()

        r = _run(build_rubric("obj", llm=FakeJudgeLLM()))
        assert r.objective_summary == "s"
        assert r.completion_conditions == ["c1", "c2"]


class _FakeJudgeLLM:
    """可编程 judge：返回固定 JSON verdict。"""

    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    async def call(self, messages, tools, model):
        self.calls += 1

        class _Msg:
            def __init__(self, content):
                self.content = content

        class _Resp:
            def __init__(self, content):
                self.choices = [type("c", (), {"message": _Msg(content)})()]
        return _Resp(self._content)


GOOD_JUDGE = _FakeJudgeLLM('{"complete": true, "confidence": 0.9, '
                           '"satisfied_conditions": ["c1"], '
                           '"evidence_quote": "FLAG{real_123}", "reasoning": "证据充分"}')


class TestVerdictChain:
    def test_anticheat_short_circuit(self, tmp_path, monkeypatch):
        job_dir = _make_job(tmp_path, "jc", ["FLAG{peeked}"], "FLAG{peeked}")
        # 插入本地答案直读事件
        events = (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events.insert(0, json.dumps({"seq": 0, "type": "tool_call",
                                     "payload": {"tool": "read_file",
                                                 "args": json.dumps({"path": "flag.txt"})}},
                                    ensure_ascii=False))
        (job_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        v = _run(evaluate_job("jc", jobs_dir=tmp_path, llm=GOOD_JUDGE,
                              ground_truth_file=None))
        assert v.complete is False
        assert v.method == "anticheat"
        assert (tmp_path / "jc" / "verdict.json").is_file()
        # verdict 事件已追加
        tail = (job_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
        assert json.loads(tail)["type"] == "verdict"

    def test_jev_high_confidence_accepted(self, tmp_path):
        _make_job(tmp_path, "jv", ["ok"], None)
        gate = JevGate(api_key="x", min_confidence=0.85)

        async def fake_decide(question, context):
            return JevDecision(answer=True, probability=0.97)

        gate.decide = fake_decide  # type: ignore[method-assign]
        v = _run(evaluate_job("jv", jobs_dir=tmp_path, llm=GOOD_JUDGE, jev=gate))
        assert v.method == "jev"
        assert v.complete is True
        assert v.confidence == 0.97

    def test_jev_low_confidence_escalates_to_judge(self, tmp_path):
        _make_job(tmp_path, "je", ["ok"], None)
        gate = JevGate(api_key="x", min_confidence=0.85)

        async def fake_decide(question, context):
            return JevDecision(answer=True, probability=0.55)

        gate.decide = fake_decide  # type: ignore[method-assign]
        v = _run(evaluate_job("je", jobs_dir=tmp_path, llm=GOOD_JUDGE, jev=gate))
        assert v.method == "llm_judge"
        assert v.complete is True
        assert v.satisfied_conditions == ["c1"]

    def test_jev_unavailable_falls_through(self, tmp_path):
        _make_job(tmp_path, "ju", ["ok"], None)
        gate = JevGate(api_key="")   # 无 key → 自动降级
        assert gate.available() is False
        v = _run(evaluate_job("ju", jobs_dir=tmp_path, llm=GOOD_JUDGE, jev=gate))
        assert v.method == "llm_judge"

    def test_ground_truth_match_short_circuit(self, tmp_path):
        _make_job(tmp_path, "jg", ["some output"], "FLAG{exact}")
        gt = tmp_path / "gt.txt"
        gt.write_text("FLAG{exact}", encoding="utf-8")
        v = _run(evaluate_job("jg", jobs_dir=tmp_path, llm=GOOD_JUDGE,
                              ground_truth_file=str(gt)))
        assert v.method == "ground_truth_match"
        assert v.complete is True
        assert v.confidence == 0.95

    def test_ground_truth_mismatch_completes_false(self, tmp_path):
        _make_job(tmp_path, "jm", ["some output"], "FLAG{wrong}")
        gt = tmp_path / "gt.txt"
        gt.write_text("FLAG{expected}", encoding="utf-8")
        bad = _FakeJudgeLLM("not json at all")
        v = _run(evaluate_job("jm", jobs_dir=tmp_path, llm=bad,
                              ground_truth_file=str(gt)))
        assert v.complete is False
        assert "不符" in v.reasoning

    def test_heuristic_fallback_when_judge_unparseable(self, tmp_path):
        _make_job(tmp_path, "jh", ["real output FLAG{x}"], "FLAG{x}")
        bad = _FakeJudgeLLM("ANSWER: FLAG{x}")   # 非 JSON → JudgeUnavailable
        v = _run(evaluate_job("jh", jobs_dir=tmp_path, llm=bad))
        assert v.method == "heuristic"
        assert v.complete is True      # 有自报答案且无作弊信号
        assert v.confidence == 0.3     # 低置信，需真实 judge 复核

    def test_heuristic_incomplete_without_answer(self, tmp_path):
        _make_job(tmp_path, "ji", ["nothing useful"], None)
        bad = _FakeJudgeLLM("garbage")
        v = _run(evaluate_job("ji", jobs_dir=tmp_path, llm=bad))
        assert v.method == "heuristic"
        assert v.complete is False
