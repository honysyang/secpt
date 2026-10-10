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
