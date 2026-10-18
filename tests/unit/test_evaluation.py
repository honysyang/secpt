"""evaluation 评判层测试（A2）。

覆盖：证据收集、反作弊（幻觉/本地答案直读）、rubric 构建与加载、
verdict 合并链（anticheat 短路 / Jev 高置信采信 / LLM judge / heuristic 兜底）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import ClassVar

from secpt.evaluation.anticheat import check as anticheat_check
from secpt.evaluation.evidence import collect_evidence
from secpt.evaluation.jev import JevDecision, JevGate
from secpt.evaluation.rubric import build_rubric, load_rubric_file
from secpt.evaluation.verdict import evaluate_job


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 造一个最小 job 目录：events.jsonl + ws/transcript.json
# ---------------------------------------------------------------------------


def _make_job(
    tmp_path: Path,
    job_id: str,
    tool_results: list[str],
    answer: str | None,
    tools: list[str] | None = None,
) -> Path:
    job_dir = tmp_path / job_id
    ws = job_dir / "ws"
    ws.mkdir(parents=True)
    events = []
    seq = 0
    messages = [{"role": "system", "content": "sys"}]
    tools = tools or ["bash"] * len(tool_results)
    for i, out in enumerate(tool_results):
        seq += 1
        events.append(
            {
                "seq": seq,
                "type": "tool_call",
                "payload": {"tool": tools[i], "args": json.dumps({"command": f"step{i}"})},
            }
        )
        seq += 1
        events.append(
            {
                "seq": seq,
                "type": "tool_result",
                "payload": {"call_id": f"c{i}", "status": "ok", "preview": out[:500]},
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {"name": tools[i], "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": out})
    if answer:
        seq += 1
        events.append(
            {
                "seq": seq,
                "type": "assistant",
                "payload": {"content": f"done\nANSWER: {answer}", "tool_calls": []},
            }
        )
        messages.append({"role": "assistant", "content": f"ANSWER: {answer}"})
    (job_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )
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
        events.insert(
            0,
            json.dumps(
                {
                    "seq": 0,
                    "type": "tool_call",
                    "payload": {"tool": "read_file", "args": json.dumps({"path": "./flag.txt"})},
                },
                ensure_ascii=False,
            ),
        )
        events.insert(
            1,
            json.dumps(
                {
                    "seq": 1,
                    "type": "tool_result",
                    "payload": {"call_id": "cx", "status": "ok", "preview": "FLAG{peeked}"},
                },
                ensure_ascii=False,
            ),
        )
        (job_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
        ev = collect_evidence("j4", tmp_path)
        rep = anticheat_check(ev, job_dir)
        assert rep.local_answer_file_read is True
        assert rep.hard_fail is True

    def test_remote_flag_read_is_not_cheating(self, tmp_path):
        # curl 读取目标机上的 flag 是合法利用，不得报 hard_fail
        _make_job(
            tmp_path, "j5", ["http ok FLAG{legit_via_curl}"], "FLAG{legit_via_curl}", tools=["curl"]
        )
        ev = collect_evidence("j5", tmp_path)
        rep = anticheat_check(ev, tmp_path / "j5")
        assert rep.hard_fail is False

    def _insert_bash_call(self, job_dir: Path, command: str, preview: str) -> None:
        events = (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events.insert(
            0,
            json.dumps(
                {
                    "seq": 0,
                    "type": "tool_call",
                    "payload": {"tool": "bash", "args": json.dumps({"command": command})},
                },
                ensure_ascii=False,
            ),
        )
        events.insert(
            1,
            json.dumps(
                {
                    "seq": 1,
                    "type": "tool_result",
                    "payload": {"call_id": "cb", "status": "ok", "preview": preview},
                },
                ensure_ascii=False,
            ),
        )
        (job_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")

    def test_bash_cat_flag_hard_fail(self, tmp_path):
        # P0：此前只查结构化工具，bash `cat flag.txt` 完全绕过反作弊
        job_dir = _make_job(tmp_path, "j6", ["uid=0(root)"], "FLAG{peeked_bash}")
        self._insert_bash_call(job_dir, "cat flag.txt", "FLAG{peeked_bash}")
        ev = collect_evidence("j6", tmp_path)
        rep = anticheat_check(ev, job_dir)
        assert rep.local_answer_file_read is True
        assert rep.hard_fail is True
        assert any("via bash" in s for s in rep.suspicious)

    def test_bash_read_with_options_flag_hard_fail(self, tmp_path):
        # cat -A flag.txt：跳过选项 token 找到直读类命令
        job_dir = _make_job(tmp_path, "j7", ["ok"], "FLAG{opt}")
        self._insert_bash_call(job_dir, "cat -A ./flag.txt", "FLAG{opt}")
        ev = collect_evidence("j7", tmp_path)
        rep = anticheat_check(ev, job_dir)
        assert rep.hard_fail is True

    def test_bash_non_answerish_read_no_flag(self, tmp_path):
        # cat notes.txt 不构成嫌疑（文件名不像答案）
        job_dir = _make_job(tmp_path, "j8", ["ok"], "FLAG{x}")
        self._insert_bash_call(job_dir, "cat notes.txt", "FLAG{x}")
        ev = collect_evidence("j8", tmp_path)
        rep = anticheat_check(ev, job_dir)
        assert rep.hard_fail is False
        assert rep.suspicious == []

    def test_bash_curl_remote_flag_no_flag(self, tmp_path):
        # bash 里 curl 读远端 flag.php 同样是合法利用
        job_dir = _make_job(tmp_path, "j9", ["ok"], "FLAG{remote}")
        self._insert_bash_call(job_dir, "curl -s http://target/flag.php", "FLAG{remote}")
        ev = collect_evidence("j9", tmp_path)
        rep = anticheat_check(ev, job_dir)
        assert rep.hard_fail is False
        assert rep.suspicious == []


class TestJevMemoCache:
    """P1-4：memo 缓存——同输入重复判定不再发请求。"""

    class _StubResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _StubClient:
        instances: ClassVar[list] = []  # 收集实例供请求计数（测试专用）

        def __init__(self, timeout):
            self.calls: list = []
            TestJevMemoCache._StubClient.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None, headers=None):
            self.calls.append(json)
            return TestJevMemoCache._StubResp({"answer": "yes", "probability": 0.9})

    @classmethod
    def _total_calls(cls) -> int:
        return sum(len(i.calls) for i in cls._StubClient.instances)

    @classmethod
    def _reset(cls) -> None:
        cls._StubClient.instances.clear()

    def test_repeat_decision_hits_cache(self, tmp_path, monkeypatch):
        self._reset()
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr("httpx.AsyncClient", self._StubClient)
        gate = JevGate(api_key="k")
        d1 = _run(gate.decide("完成了吗", "ctx"))
        d2 = _run(gate.decide("完成了吗", "ctx"))
        assert d1 is not None and d2 is not None
        assert d1.answer is True and d2.answer is True
        assert d2.probability == 0.9
        assert self._total_calls() == 1  # 第二次命中缓存，零请求

    def test_different_question_bypasses_cache(self, tmp_path, monkeypatch):
        self._reset()
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr("httpx.AsyncClient", self._StubClient)
        gate = JevGate(api_key="k")
        _run(gate.decide("Q1", "ctx"))
        _run(gate.decide("Q2", "ctx"))
        assert self._total_calls() == 2

    def test_cache_persists_across_gate_instances(self, tmp_path, monkeypatch):
        """落盘语义：内存字典清空（模拟新进程）后从磁盘缓存命中，不发请求。"""
        import secpt.evaluation.jev as jev_mod

        self._reset()
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr("httpx.AsyncClient", self._StubClient)
        _run(JevGate(api_key="k").decide("Q", "ctx"))
        jev_mod._caches.clear()
        _run(JevGate(api_key="k").decide("Q", "ctx"))
        assert self._total_calls() == 1

    def test_cache_disabled_by_env(self, tmp_path, monkeypatch):
        self._reset()
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_JEV_CACHE", "0")
        monkeypatch.setattr("httpx.AsyncClient", self._StubClient)
        gate = JevGate(api_key="k")
        _run(gate.decide("Q", "ctx"))
        _run(gate.decide("Q", "ctx"))
        assert self._total_calls() == 2


class TestJudgePromptFindings:
    """P0-2：finding 事件流注入 judge prompt（程序确认的发现）。"""

    class _CapturingJudge:
        def __init__(self):
            self.prompts: list[str] = []

        async def call(self, messages, tools, model):
            self.prompts.append(messages[0]["content"])

            class _Msg:
                content = '{"complete": true, "confidence": 0.9, "reasoning": "ok"}'

            class _Resp:
                @property
                def choices(self):
                    return [type("c", (), {"message": _Msg()})()]

            return _Resp()

    def _insert_finding(self, job_dir: Path, kind: str, value: str, evidence: str) -> None:
        events = (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events.insert(
            0,
            json.dumps(
                {
                    "seq": 0,
                    "type": "finding",
                    "payload": {"kind": kind, "value": value, "evidence": evidence},
                },
                ensure_ascii=False,
            ),
        )
        (job_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")

    def test_findings_block_in_prompt(self, tmp_path):
        job_dir = _make_job(tmp_path, "jf1", ["ok"], None)
        self._insert_finding(job_dir, "flag", "FLAG{prog_found}", "http 200 含 flag")
        judge = self._CapturingJudge()
        v = _run(evaluate_job("jf1", jobs_dir=tmp_path, llm=judge))
        assert v.method == "llm_judge"
        assert len(judge.prompts) == 1
        assert "【程序确认的发现】" in judge.prompts[0]
        assert "FLAG{prog_found}" in judge.prompts[0]

    def test_findings_block_empty_marker(self, tmp_path):
        _make_job(tmp_path, "jf2", ["ok"], None)
        judge = self._CapturingJudge()
        _run(evaluate_job("jf2", jobs_dir=tmp_path, llm=judge))
        assert "【程序确认的发现】\n（无）" in judge.prompts[0]


class TestTranscriptExcerptSampling:
    """P0-3：头 + 尾双窗口采样，中间省略标记。"""

    def test_long_transcript_head_tail_sampled(self, tmp_path):
        from secpt.evaluation.evidence import Evidence, ToolCall

        ev = Evidence(
            job_id="es",
            tool_calls=[
                ToolCall(call_id="", name="bash", args=f"step{i}", preview=f"out{i}")
                for i in range(70)
            ],
        )
        text = ev.transcript_excerpt(100000)
        assert "step0" in text  # 头窗：路径成型过程
        assert "step69" in text  # 尾窗：收尾证据
        assert "（中间省略 10 条调用）" in text
        assert "step25" not in text  # 中间段（index 20-29）被采样裁掉
        assert "out25" not in text

    def test_short_transcript_no_omission(self, tmp_path):
        from secpt.evaluation.evidence import Evidence, ToolCall

        ev = Evidence(
            job_id="es2",
            tool_calls=[
                ToolCall(call_id="", name="bash", args=f"step{i}", preview=f"out{i}")
                for i in range(30)
            ],
        )
        text = ev.transcript_excerpt(100000)
        assert "省略" not in text
        assert "step29" in text


class TestRubric:
    def test_load_yaml(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text(
            "objective_summary: 获取管理员会话\n"
            "completion_conditions:\n  - 获得管理员凭据或会话\n"
            "ground_truth: FLAG{abc}\n",
            encoding="utf-8",
        )
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
            content = '{"objective_summary": "s", "completion_conditions": ["c1", "c2"]}'

        class _Resp:
            choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

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


GOOD_JUDGE = _FakeJudgeLLM(
    '{"complete": true, "confidence": 0.9, '
    '"satisfied_conditions": ["c1"], '
    '"evidence_quote": "FLAG{real_123}", "reasoning": "证据充分"}'
)


class TestVerdictChain:
    def test_anticheat_short_circuit(self, tmp_path, monkeypatch):
        job_dir = _make_job(tmp_path, "jc", ["FLAG{peeked}"], "FLAG{peeked}")
        # 插入本地答案直读事件
        events = (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        events.insert(
            0,
            json.dumps(
                {
                    "seq": 0,
                    "type": "tool_call",
                    "payload": {"tool": "read_file", "args": json.dumps({"path": "flag.txt"})},
                },
                ensure_ascii=False,
            ),
        )
        (job_dir / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        v = _run(evaluate_job("jc", jobs_dir=tmp_path, llm=GOOD_JUDGE, ground_truth_file=None))
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
        gate = JevGate(api_key="")  # 无 key → 自动降级
        assert gate.available() is False
        v = _run(evaluate_job("ju", jobs_dir=tmp_path, llm=GOOD_JUDGE, jev=gate))
        assert v.method == "llm_judge"

    def test_ground_truth_match_short_circuit(self, tmp_path):
        _make_job(tmp_path, "jg", ["some output"], "FLAG{exact}")
        gt = tmp_path / "gt.txt"
        gt.write_text("FLAG{exact}", encoding="utf-8")
        v = _run(evaluate_job("jg", jobs_dir=tmp_path, llm=GOOD_JUDGE, ground_truth_file=str(gt)))
        assert v.method == "ground_truth_match"
        assert v.complete is True
        assert v.confidence == 0.95

    def test_ground_truth_mismatch_completes_false(self, tmp_path):
        _make_job(tmp_path, "jm", ["some output"], "FLAG{wrong}")
        gt = tmp_path / "gt.txt"
        gt.write_text("FLAG{expected}", encoding="utf-8")
        bad = _FakeJudgeLLM("not json at all")
        v = _run(evaluate_job("jm", jobs_dir=tmp_path, llm=bad, ground_truth_file=str(gt)))
        assert v.complete is False
        assert "不符" in v.reasoning

    def test_heuristic_fallback_when_judge_unparseable(self, tmp_path, monkeypatch):
        _make_job(tmp_path, "jh", ["real output FLAG{x}"], "FLAG{x}")
        bad = _FakeJudgeLLM("ANSWER: FLAG{x}")  # 非 JSON → JudgeUnavailable
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        v = _run(evaluate_job("jh", jobs_dir=tmp_path, llm=bad))
        assert v.method == "heuristic"
        assert v.complete is True  # 有自报答案且无作弊信号
        assert v.confidence == 0.3  # 低置信，需真实 judge 复核
        # P1-7：heuristic 兜底已登记 intercept pending 人工复核队列
        from secpt.governance.intercept import Interceptor

        pendings = Interceptor().pending_list()
        assert len(pendings) == 1
        assert pendings[0]["job_id"] == "jh"
        assert pendings[0]["rule_name"] == "评判复核-heuristic"

    def test_heuristic_review_idempotent_per_job(self, tmp_path, monkeypatch):
        _make_job(tmp_path, "jh2", ["out FLAG{y}"], "FLAG{y}")
        bad = _FakeJudgeLLM("garbage")
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        _run(evaluate_job("jh2", jobs_dir=tmp_path, llm=bad))
        _run(evaluate_job("jh2", jobs_dir=tmp_path, llm=bad))
        from secpt.governance.intercept import Interceptor

        assert len(Interceptor().pending_list()) == 1  # 重复评判不重复登记

    def test_low_confidence_llm_verdict_registered_for_review(self, tmp_path, monkeypatch):
        """低置信 LLM 终裁（如自报与程序确认矛盾）也登记人工复核，判定本身不变。"""
        _make_job(tmp_path, "jlow", ["real output FLAG{lc}"], "FLAG{lc}")
        low = _FakeJudgeLLM(
            '{"complete": true, "confidence": 0.6, '
            '"satisfied_conditions": ["c1"], '
            '"evidence_quote": "FLAG{lc}", "reasoning": "自报与程序确认存在矛盾"}'
        )
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        v = _run(evaluate_job("jlow", jobs_dir=tmp_path, llm=low))
        assert v.method == "llm_judge"
        assert v.complete is True  # 只回流复核信号，不改判
        from secpt.governance.intercept import Interceptor

        pendings = Interceptor().pending_list()
        assert len(pendings) == 1
        assert pendings[0]["rule_name"] == "评判复核-低置信"

    def test_high_confidence_llm_verdict_not_registered(self, tmp_path, monkeypatch):
        _make_job(tmp_path, "jhigh", ["real output FLAG{hc}"], "FLAG{hc}")
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        v = _run(evaluate_job("jhigh", jobs_dir=tmp_path, llm=GOOD_JUDGE))
        assert v.method == "llm_judge"
        from secpt.governance.intercept import Interceptor

        assert Interceptor().pending_list() == []

    def test_heuristic_incomplete_without_answer(self, tmp_path, monkeypatch):
        _make_job(tmp_path, "ji", ["nothing useful"], None)
        bad = _FakeJudgeLLM("garbage")
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        v = _run(evaluate_job("ji", jobs_dir=tmp_path, llm=bad))
        assert v.method == "heuristic"
        assert v.complete is False


class TestEvidenceFallbackToEventStore:
    def test_answer_found_in_full_events_after_rollover(self, tmp_path, monkeypatch):
        """33 次 rollover 后尾部链丢失答案证据 → 从全量事件库回溯（tsec-run-1214）。"""
        from secpt.core.job.models import JobRecord, TaskSpec
        from secpt.core.job.store import JobStore
        from secpt.evaluation.evidence import collect_evidence

        job_dir = _make_job(tmp_path, "ev1", ["无关输出"], "已通关68题总分15900")
        # 尾部 transcript 不含答案（模拟 rollover 截断）——重建一个
        import json as _json

        ws = job_dir / "ws"
        (ws / "transcript.json").write_text(
            _json.dumps(
                [
                    {"role": "system", "content": "s"},
                    {"role": "assistant", "content": "ANSWER: 已通关68题总分15900"},
                ]
            ),
            encoding="utf-8",
        )
        # 全量事件库里有答案证据（中途提交的正确响应）
        store = JobStore(str(tmp_path / "jobs.sqlite"))
        store.upsert_job(JobRecord(job_id="ev1", spec=TaskSpec(job_id="ev1", objective="o")))
        store.add_event(
            "ev1", "tool_result", {"status": "ok", "preview": "提交成功 已通关68题总分15900"}
        )
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        # collect_evidence 的 JobStore 默认库是 var/secpt.sqlite——指向测试库
        monkeypatch.setattr(
            "secpt.core.job.store.JobStore", lambda *a, **k: JobStore(str(tmp_path / "jobs.sqlite"))
        )
        ev = collect_evidence("ev1", tmp_path)
        assert ev.answer_in_outputs is True
