"""learning 闭环测试：蒸馏入库 / 指纹去重 / 检索注入。"""

from __future__ import annotations

import json
from pathlib import Path

from secpt.learning.distill import distill_job
from secpt.learning.retrieve import experience_section, search_experiences
from secpt.learning.store import ExperienceJournal, LearnedExperience


def _mk_job(tmp_path: Path, job_id: str, transcript: list[dict]) -> Path:
    job_dir = tmp_path / job_id
    (job_dir / "ws").mkdir(parents=True)
    (job_dir / "ws" / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
    )
    (job_dir / "events.jsonl").write_text("", encoding="utf-8")
    return job_dir


class _DistillLLM:
    def __init__(self, content: str):
        self._content = content

    async def call(self, messages, tools, model):
        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Resp:
            def __init__(self, c):
                self.choices = [type("c", (), {"message": _Msg(c)})()]

        return _Resp(self._content)


_GOOD = json.dumps(
    {
        "title": "SAML XSW 绕过签名",
        "technique": "saml_xsw",
        "indicators": ["saml", "sso", "签名"],
        "what_worked": "注入无签名断言让 SP 取到伪造 NameID",
        "pitfalls": ["直接改签名断言会失败"],
        "outcome": "success",
    },
    ensure_ascii=False,
)


class TestJournal:
    def test_append_and_dedup(self, tmp_path):
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        exp = LearnedExperience(
            title="t", technique="saml_xsw", what_worked="do x", outcome="success"
        )
        assert jr.append(exp) is True
        assert jr.append(exp) is False  # 同指纹去重
        assert len(jr.load()) == 1

    def test_load_corrupt_line_skipped(self, tmp_path):
        p = tmp_path / "exp.jsonl"
        p.write_text(
            '{"bad json\n' + json.dumps(LearnedExperience(title="ok").to_dict()) + "\n",
            encoding="utf-8",
        )
        jr = ExperienceJournal(p)
        assert len(jr.load()) == 1


class TestDistill:
    def test_distill_success(self, tmp_path):
        import asyncio

        _mk_job(
            tmp_path,
            "j1",
            [
                {"role": "assistant", "content": "尝试 XSW"},
                {"role": "tool", "content": "200 ok"},
            ],
        )
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        exp = asyncio.new_event_loop().run_until_complete(
            distill_job("j1", llm=_DistillLLM(_GOOD), jobs_dir=tmp_path, journal=jr)
        )
        assert exp is not None and exp.technique == "saml_xsw"
        assert jr.load()[0].title == "SAML XSW 绕过签名"

    def test_distill_unparseable_returns_none(self, tmp_path):
        import asyncio

        _mk_job(tmp_path, "j2", [{"role": "assistant", "content": "x"}])
        exp = asyncio.new_event_loop().run_until_complete(
            distill_job(
                "j2",
                llm=_DistillLLM("不是 json"),
                jobs_dir=tmp_path,
                journal=ExperienceJournal(tmp_path / "e.jsonl"),
            )
        )
        assert exp is None

    def test_distill_never_raises(self, tmp_path):
        import asyncio

        exp = asyncio.new_event_loop().run_until_complete(
            distill_job(
                "nonexistent",
                llm=None,
                jobs_dir=tmp_path,
                journal=ExperienceJournal(tmp_path / "e.jsonl"),
            )
        )
        assert exp is None


class TestRetrieve:
    def _seed(self, tmp_path):
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(
            LearnedExperience(
                title="SAML XSW 绕过签名",
                technique="saml_xsw",
                indicators=["saml", "sso"],
                what_worked="伪造断言",
                pitfalls=["别改签名"],
                outcome="success",
            )
        )
        jr.append(
            LearnedExperience(
                title="SQL 注入拿数据",
                technique="sqli",
                indicators=["sql"],
                what_worked="union select",
                outcome="success",
            )
        )
        return jr

    def test_search_relevance(self, tmp_path):
        jr = self._seed(tmp_path)
        hits = search_experiences("目标使用 SAML 单点登录", journal=jr)
        assert hits and hits[0].technique == "saml_xsw"

    def test_section_render_and_switch(self, tmp_path, monkeypatch):
        jr = self._seed(tmp_path)
        sec = experience_section("saml sso 登录", journal=jr)
        assert "历史经验" in sec and "SAML XSW" in sec
        monkeypatch.setenv("SECPT_EXPERIENCE", "0")
        assert experience_section("saml", journal=jr) == ""

    def test_no_match_empty(self, tmp_path):
        jr = self._seed(tmp_path)
        assert experience_section("完全无关的内容 xyz", journal=jr) == ""

    def test_short_indicator_no_substring_noise(self, tmp_path):
        """⑥ 2 字符指标不得以子串命中英文单词（'SP' 曾误中 resp/response）。"""
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(
            LearnedExperience(title="SAML SSO 越权", technique="saml_sso", indicators=["SP", "IdP"])
        )
        # benchmark 模板里满是 resp/response → 旧实现对 "SP" 子串命中而误注入
        obj = "resp = urllib.request.urlopen(req)  response_body = resp.read()"
        assert search_experiences(obj, journal=jr) == []

    def test_task_type_gate_weak_match(self, tmp_path):
        """⑥ 跨任务类型仅采信强信号：仅指标命中（弱）不注入。"""
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(
            LearnedExperience(
                # 标题不含查询会命中的词——否则按修复后的语义算强信号（见下个测试）
                title="某通用渗透手法",
                technique="generic_tactic",
                indicators=["saml"],
                task_type="ctf",
            )
        )
        # benchmark 任务里出现 "saml"（仅指标弱信号）→ 跨域不注入
        assert search_experiences("目标使用 saml 登录", journal=jr, task_type="benchmark") == []
        # 同任务类型（ctf）→ 正常注入
        hits = search_experiences("目标使用 saml 登录", journal=jr, task_type="ctf")
        assert hits and hits[0].technique == "generic_tactic"

    def test_task_type_gate_strong_crosses(self, tmp_path):
        """⑥ 跨任务类型但强信号（技术名命中）仍可注入。"""
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(LearnedExperience(title="某题", technique="saml_xsw", task_type="ctf"))
        hits = search_experiences("尝试 saml_xsw 手法", journal=jr, task_type="benchmark")
        assert hits and hits[0].technique == "saml_xsw"

    def test_title_term_counts_strong(self, tmp_path):
        """修复回归：标题显著词命中是强信号（旧实现 `q in title` 方向反了，分支恒不触发）。"""
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(LearnedExperience(title="SAML XSW 绕过签名", technique="xsw", task_type="ctf"))
        # 标题词 "saml" 命中 benchmark 任务 → 强信号，跨域注入
        hits = search_experiences("目标使用 saml 单点登录", journal=jr, task_type="benchmark")
        assert hits and hits[0].technique == "xsw"

    def test_filler_words_do_not_match(self, tmp_path):
        """标题里的 ASCII 填充词不产生命中（防强信号放大噪声）。"""
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(LearnedExperience(title="the and get 手法", technique="t", task_type="ctf"))
        assert search_experiences("get the flag for this and use it", journal=jr) == []


class TestUsageFeedback:
    """C 反馈闭环：注入→结果回写（usage 旁路事件流 + 检索命中率因子）。"""

    def test_record_and_load_usage(self, tmp_path):
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        assert jr.record_usage("exp-1", True) is True
        assert jr.record_usage("exp-1", False) is True
        assert jr.record_usage("exp-2", True) is True
        assert jr.record_usage("", True) is False  # 空 id 拒绝
        assert jr.load_usage() == {"exp-1": (1, 1), "exp-2": (1, 0)}

    def test_usage_corrupt_line_skipped(self, tmp_path):
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.record_usage("exp-1", True)
        with open(jr.usage_path, "a", encoding="utf-8") as f:
            f.write("{broken json\n")
        assert jr.load_usage() == {"exp-1": (1, 0)}

    def test_no_usage_file_neutral(self, tmp_path):
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        assert jr.load_usage() == {}

    def test_usage_factor_bounds_and_shrink(self):
        from secpt.learning.retrieve import _usage_factor

        assert _usage_factor(0, 0) == 1.0
        assert _usage_factor(10, 0) > 1.0  # 全中升权
        assert _usage_factor(0, 10) < 1.0  # 全挂降权
        # 小样本收缩：n=1 全中的偏移量必须小于 n=10 全中（防一次偶然命中过度升权）
        assert abs(_usage_factor(1, 0) - 1.0) < abs(_usage_factor(10, 0) - 1.0)

    def test_miss_history_demotes_ranking(self, tmp_path):
        """等强信号两条经验：有挂史的被降权（排序翻转）。"""
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        exp_a = LearnedExperience(
            title="SAML XSW 手法甲",
            technique="saml_xsw",
            what_worked="手法甲",
            indicators=["saml"],
        )
        exp_b = LearnedExperience(
            title="SAML XSW 手法乙",
            technique="saml_xsw",
            what_worked="手法乙",
            indicators=["saml"],
        )
        jr.append(exp_a)
        jr.append(exp_b)
        hits0 = search_experiences("saml 登录", journal=jr)
        assert len(hits0) == 2
        first = hits0[0].id
        for _ in range(6):
            jr.record_usage(first, False)
        hits1 = search_experiences("saml 登录", journal=jr)
        assert len(hits1) == 2
        assert hits1[0].id != first  # 挂史 6 次的降到后面

    def test_feedback_helper_writes_back_dedup(self, tmp_path, monkeypatch):
        """_feedback_experiences：stages/frontier 子 job 的注入记录聚合去重回写。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.cli.solve import _feedback_experiences
        from secpt.core.job.resume import jobs_dir

        jdir = jobs_dir() / "jobx"
        jdir.mkdir(parents=True)
        (jdir / "experiences_injected.json").write_text(
            json.dumps(["exp-a", "exp-b"]), encoding="utf-8"
        )
        sub = jobs_dir() / "jobx-irecon1"
        sub.mkdir()
        (sub / "experiences_injected.json").write_text(
            json.dumps(["exp-b", "exp-c"]), encoding="utf-8"
        )
        n = _feedback_experiences("jobx", outcome=True)
        assert n == 3  # a/b/c 去重后各一次
        assert ExperienceJournal().load_usage() == {
            "exp-a": (1, 0),
            "exp-b": (1, 0),
            "exp-c": (1, 0),
        }

    def test_engine_runner_records_injection(self, tmp_path, monkeypatch):
        """run_engine 注入经验时落 experiences_injected.json（回写的依据）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_FAKE_LLM", "1")
        jr = ExperienceJournal()  # SECPT_VAR 已在调用时生效（懒解析）
        jr.append(
            LearnedExperience(title="SAML XSW 绕过签名", technique="saml_xsw", indicators=["saml"])
        )
        from secpt.cli.engine_runner import run_engine

        run_engine(objective="目标使用 saml 单点登录", profile="ctf", job_id="inj1", max_steps=3)
        rec = tmp_path / "jobs" / "inj1" / "experiences_injected.json"
        assert rec.is_file()
        assert jr.load()[0].id in json.loads(rec.read_text(encoding="utf-8"))
