"""图记忆派测试：联想召回 / 程序自动写图 / 冷节点压缩（签名缓存）。"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

from secpt.intel.maintain import maintain
from secpt.intel.scribe import GraphScribe
from secpt.intel.store import IntelStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestAssociativeRecall:
    def test_keyword_hit_brings_neighbors(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        f1 = st.add_node("fact", "登录接口无验证码")
        f2 = st.add_node("fact", "口令策略仅 6 位")
        finding = st.add_node("finding", "可暴力破解管理员账号")
        st.add_edge(f1, finding, "derived_from")
        st.add_edge(f2, finding, "derived_from")

        hits = st.recall_graph("暴力破解")
        ids = {h["id"] for h in hits}
        assert finding in ids
        # 联想：与 finding 血缘相连的两个 fact 一并带回（无需关键词命中它们）
        assert f1 in ids and f2 in ids
        assoc = [h for h in hits if h.get("associated")]
        assert len(assoc) == 2

    def test_cold_excluded_from_recall(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        n = st.add_node("fact", "旧观察 SAML 端点存在")
        st.mark_cold([n])
        assert st.recall("SAML") == []
        assert st.recall_graph("SAML") == []


class TestGraphScribe:
    def _hooks(self, tmp_path):
        captured = []

        class Inner:
            def on_finding(self, kind, value, evidence=""):
                captured.append((kind, value))

        return GraphScribe(Inner(), job_id="j1", store=IntelStore(tmp_path / "i.db")), captured

    def test_asset_url_observed(self, tmp_path):
        hooks, captured = self._hooks(tmp_path)
        hooks.on_finding("asset", "http://target.example.com/login")
        st = IntelStore(tmp_path / "i.db")
        assert st.find_asset("endpoint", "http://target.example.com/login") is not None
        assert captured == [("asset", "http://target.example.com/login")]

    def test_credential_becomes_fact(self, tmp_path):
        hooks, _ = self._hooks(tmp_path)
        hooks.on_finding("credential", "password=admin123")
        hits = IntelStore(tmp_path / "i.db").recall("凭据")
        assert hits and "admin123" in hits[0]["content"]

    def test_inner_exception_does_not_break(self, tmp_path):
        hooks, _ = self._hooks(tmp_path)
        hooks.on_finding("flag", "FLAG{x}")  # 非 asset/credential → 不落图但不炸
        st = IntelStore(tmp_path / "i.db")
        assert st.assets() == []


class _SumLLM:
    """可编程摘要 LLM。"""

    def __init__(self):
        self.calls = 0

    async def call(self, m, t, model):
        self.calls += 1

        class _Msg:
            content = "旧观察摘要：多个端点存在弱认证"

        class _Resp:
            choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

        return _Resp()


class TestColdMaintain:
    def _old_facts(self, st, n=5, days=10):
        ids = []
        old = time.time() - days * 86400
        for i in range(n):
            # 直接改 ts 模拟旧节点
            nid = st.add_node("fact", f"旧观察 {i}：端点 /api/v{i} 弱认证")
            with st._conn() as c:
                c.execute("UPDATE nodes SET ts=? WHERE id=?", (old, nid))
            ids.append(nid)
        return ids

    def test_compress_and_recall_excludes(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        self._old_facts(st, n=5)
        llm = _SumLLM()
        r = _run(maintain(st, llm=llm, min_group=3))
        assert r["compressed"] == 5
        assert llm.calls == 1
        # 冷摘要节点存在，原节点不再被 recall
        hits = st.recall("弱认证")
        assert all("冷摘要" in h["content"] for h in hits)
        assert st.recall("旧观察 0") == []

    def test_signature_cache_skips_llm(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        self._old_facts(st, n=4)
        llm1, llm2 = _SumLLM(), _SumLLM()
        r1 = _run(maintain(st, llm=llm1, min_group=3))
        assert r1["compressed"] == 4
        # 再次维护：同样的组签名命中缓存，零 LLM 调用
        r2 = _run(maintain(st, llm=llm2, min_group=3))
        assert r2["compressed"] == 0 and r2["cached"] == 0  # 已无候选（都 cold 了）
        assert llm2.calls == 0

    def test_small_group_skipped(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        self._old_facts(st, n=2)
        llm = _SumLLM()
        r = _run(maintain(st, llm=llm, min_group=3))
        assert r["skipped"] == 2 and llm.calls == 0

    def test_no_llm_noop(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        self._old_facts(st, n=5)
        r = _run(maintain(st, llm=None))
        assert r["compressed"] == 0
