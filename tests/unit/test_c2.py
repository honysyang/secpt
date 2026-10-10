"""C2 狩猎专项测试：CVE 去重（防 1day 当 0day）/ LLM 池故障转移。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from secpt.findings.cve import _valid_cve, dedup_known
from secpt.runtime.llm import pool


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Msg:
    def __init__(self, c):
        self.content = c


class _Resp:
    def __init__(self, c):
        self.choices = [type("c", (), {"message": _Msg(c)})()]


class _CveLLM:
    def __init__(self, content):
        self._c = content

    async def call(self, m, t, model):
        return _Resp(self._c)


class TestCveDedup:
    def test_valid_cve_format(self):
        assert _valid_cve("CVE-2021-44228")
        assert not _valid_cve("CVE-999-1")
        assert not _valid_cve("CVE-3000-1")
        assert not _valid_cve("not-a-cve")

    def test_known_cve_detected(self):
        llm = _CveLLM(json.dumps({"cve_candidates": ["cve-2021-44228"], "note": "log4shell"}))
        out = _run(dedup_known("RCE", "JNDI 注入", llm=llm))
        assert out == ["CVE-2021-44228"]  # 大小写归一

    def test_hallucinated_cve_rejected(self):
        llm = _CveLLM(json.dumps({"cve_candidates": ["CVE-2021-99999999"]}))
        assert _run(dedup_known("x", "y", llm=llm)) == []

    def test_no_llm_returns_empty(self):
        assert _run(dedup_known("x", "y", llm=None)) == []

    def test_skill_manual_exists(self):
        p = Path(__file__).resolve().parents[2] / "knowledge/skills/api-recon/SKILL.md"
        text = p.read_text(encoding="utf-8")
        assert "完成定义" in text and "流程门禁" in text and "pitfalls" in text.lower()


class TestLLMPool:
    def test_pool_disabled_single_call(self, monkeypatch):
        monkeypatch.delenv("SECPT_MODEL_POOL", raising=False)
        assert pool.pool_models() == []

    def test_failover_chain(self, monkeypatch):
        monkeypatch.setenv("SECPT_MODEL_POOL", "m1,m2")
        calls = []

        async def fake_call_llm(messages, tools, model, api_key=None, fallback=None):
            calls.append(model)
            if model == "m1":
                raise ConnectionError("down")
            return _Resp("ok")

        monkeypatch.setattr(pool, "call_llm", fake_call_llm)
        resp = _run(pool.call_pooled([], [], "m1"))
        assert resp.choices[0].message.content == "ok"
        assert calls == ["m1", "m2"]  # 故障转移到 m2

    def test_all_failed_raises(self, monkeypatch):
        monkeypatch.setenv("SECPT_MODEL_POOL", "m1,m2")

        async def fake_call_llm(messages, tools, model, api_key=None, fallback=None):
            raise ConnectionError("all down")

        monkeypatch.setattr(pool, "call_llm", fake_call_llm)
        with __import__("pytest").raises(ConnectionError):
            _run(pool.call_pooled([], [], "m1"))

    def test_circuit_breaker(self, monkeypatch):
        monkeypatch.setenv("SECPT_MODEL_POOL", "bad,good")

        async def fake_call_llm(messages, tools, model, api_key=None, fallback=None):
            if model == "bad":
                raise ConnectionError("down")
            return _Resp("ok")

        monkeypatch.setattr(pool, "call_llm", fake_call_llm)
        import contextlib

        for _ in range(3):  # bad 连续失败 3 次 → 熔断
            with contextlib.suppress(ConnectionError):
                _run(pool.call_pooled([], [], "bad"))
        assert pool._broken("bad")
        # 熔断后直跳 good
        calls = []

        async def spy(messages, tools, model, api_key=None, fallback=None):
            calls.append(model)
            return _Resp("ok")

        monkeypatch.setattr(pool, "call_llm", spy)
        _run(pool.call_pooled([], [], "bad"))
        assert calls == ["good"]
        pool._circuits.clear()
