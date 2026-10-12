"""B2 双图记忆测试：资产图（程序算关系）/ 探索血缘 / 覆盖度 / 检索工具。"""

from __future__ import annotations

import asyncio

import pytest

from secpt.intel.store import IntelStore, normalize_asset
from secpt.intel.tools import make_intel_tools


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestNormalize:
    @pytest.mark.parametrize(
        "atype,value,expect_key",
        [
            ("endpoint", "http://A.example.com/x?y=1", "endpoint:a.example.com/x"),
            ("endpoint", "http://a.example.com/x", "endpoint:a.example.com/x"),
            ("root_domain", "example.com/", "root_domain:example.com"),
            ("ip", "192.168.1.1", "ip:192.168.1.1"),
            ("service", "http://a.com:8080", "service:a.com:8080"),
        ],
    )
    def test_dedup_key(self, atype, value, expect_key):
        assert normalize_asset(atype, value) == expect_key


class TestAssetGraph:
    def test_observe_dedup_and_parent(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        root = st.observe_asset("root_domain", "example.com", job_id="j1")
        again = st.observe_asset("root_domain", "http://example.com/", job_id="j2")
        assert root == again  # 程序去重，大小写/尾斜杠/协议不敏感
        sub = st.observe_asset("subdomain", "a.example.com", parent_id=root)
        assets = st.assets()
        assert len(assets) == 2
        parented = next(a for a in assets if a["id"] == sub)
        assert parented["parent_id"] == root

    def test_bad_type_rejected(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        with pytest.raises(ValueError):
            st.observe_asset("host", "x")


class TestExploration:
    def test_lineage(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        goal = st.add_node("goal", "拿下目标", job_id="j1")
        fact1 = st.add_node("fact", "登录接口无验证码")
        fact2 = st.add_node("fact", "口令策略 6 位")
        finding = st.add_node("finding", "可暴力破解")
        st.add_edge(goal, fact1, "spawns")
        st.add_edge(goal, fact2, "spawns")
        st.add_edge(fact1, finding, "derived_from")
        st.add_edge(fact2, finding, "derived_from")
        lin = st.lineage(finding)
        assert len(lin["derived_from"]) == 2  # 两个 fact 综合得出
        kinds = {e["kind"] for e in lin["derived_from"]}
        assert kinds == {"derived_from"}

    def test_record_goal_idempotent(self, tmp_path):
        """Goals 层：子目标落图幂等——同文本只建一次，跨任务复用。"""
        st = IntelStore(tmp_path / "i.db")
        g1 = st.record_goal("拿到 admin 凭据", job_id="j1")
        g2 = st.record_goal("拿到 admin 凭据", job_id="j2")
        assert g1 == g2
        g3 = st.record_goal("横向移动到内网")
        assert g3 != g1
        with pytest.raises(ValueError):
            st.record_goal("   ")


class TestRecall:
    def test_fts_or_like(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        st.add_node("fact", "SAML Response 的 Assertion 未校验签名", job_id="j1")
        st.add_node("hint", "尝试 XPath 注入无果", job_id="j1")
        hits = st.recall("SAML 签名")
        assert any("SAML" in h["content"] for h in hits)
        hits2 = st.recall("XPath")
        assert hits2 and hits2[0]["type"] == "hint"


class TestCoverage:
    def test_coverage_and_untested(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        a1 = st.observe_asset("endpoint", "http://t/login")
        st.observe_asset("endpoint", "http://t/admin")
        n1 = st.add_node("fact", "测试了登录", anchor_assets=[a1])
        assert n1 > 0
        cov = st.coverage()["endpoint"]
        assert cov["total"] == 2 and cov["touched"] == 1
        untested = st.untested_assets(atype="endpoint")
        assert len(untested) == 1 and "admin" in untested[0]["value"]


class TestTools:
    def test_tool_flow(self, tmp_path):
        st = IntelStore(tmp_path / "i.db")
        tools = make_intel_tools(st, job_id="j1")
        assert set(tools) == {"observe_asset", "record_fact", "recall", "search_traces"}

        out = _run(tools["observe_asset"][1]({"asset_type": "endpoint", "value": "http://t/x"}))
        assert out.startswith("OK: asset#")

        out = _run(tools["record_fact"][1]({"node_type": "fact", "content": "目录可遍历"}))
        assert out.startswith("OK: node#")

        out = _run(tools["recall"][1]({"query": "目录"}))
        assert "目录可遍历" in out

        # 独立容错：坏输入返回 Error 不抛
        out = _run(tools["observe_asset"][1]({"asset_type": "bad", "value": "x"}))
        assert out.startswith("Error:")
