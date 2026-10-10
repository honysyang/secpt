"""C1 交付测试：CWE 分类（防幻觉）/ 报告生成 / 证据一致性快照。"""

from __future__ import annotations

import asyncio
import json
import zipfile
from typing import ClassVar

import pytest

from secpt.findings.models import EvidenceRef, Finding
from secpt.findings.store import FindingStore
from secpt.report import ReportDelivery, classify, format_cwe
from secpt.report.cwe import CWE_DICT, candidates


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk(
    title="SAML 签名包装越权", desc="注入伪造断言绕过签名校验", severity="high", **kw
) -> Finding:
    return Finding(
        title=title,
        description=desc,
        severity=severity,
        target="http://t/",
        vuln_class="saml_xsw",
        reproduction="1.抓包 2.改断言 3.重放",
        **kw,
    )


class TestCWE:
    def test_candidates_keyword(self):
        cands = candidates("SAML 签名校验存在签名包装问题")
        assert any(c == "CWE-347" for c, _ in cands)

    def test_candidates_sqli(self):
        assert candidates("union select 注入")[0][0] == "CWE-89"

    def test_classify_without_llm_falls_back(self):
        out = _run(classify("SQL 注入", "union select 注入点"))
        assert out and out[0]["cwe"] in CWE_DICT

    def test_classify_llm_picks_and_validates(self):
        class _Msg:
            content = json.dumps([{"cwe": "CWE-347", "rationale": "签名包装"}])

        class _Resp:
            choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

        class FakeLLM:
            async def call(self, m, t, model):
                return _Resp()

        out = _run(classify("SAML 问题", "签名包装绕过", llm=FakeLLM()))
        assert out[0]["cwe"] == "CWE-347"
        assert "签名" in out[0]["name"]

    def test_classify_llm_hallucination_rejected(self):
        """幻觉编号必须被字典校验丢弃。"""

        class _Msg:
            content = json.dumps([{"cwe": "CWE-999999", "rationale": "编造的"}])

        class _Resp:
            choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

        class FakeLLM:
            async def call(self, m, t, model):
                return _Resp()

        out = _run(classify("SAML", "签名包装", llm=FakeLLM()))
        assert all(c["cwe"] in CWE_DICT for c in out)
        # CWE-999999 被丢弃后回退关键词
        assert any(c["cwe"] == "CWE-347" for c in out)

    def test_format_cwe(self):
        s = format_cwe([{"cwe": "CWE-89", "name": "SQL注入"}])
        assert s.startswith("CWE-89")


class TestReport:
    def _seed(self, tmp_path) -> FindingStore:
        st = FindingStore(tmp_path / "f.db")
        f1 = st.save(_mk(title="XSW 越权", job_id="j1", first_seen={"job_id": "j1", "step": 12}))
        st.record_reproduction(f1.id, job_id="v1", verdict="reproduced")
        st.save(
            _mk(
                title="信息泄露",
                severity="low",
                job_id="j1",
                first_seen={"job_id": "j1", "step": 3},
            )
        )
        return st

    def test_markdown_report(self, tmp_path):
        st = self._seed(tmp_path)
        rd = ReportDelivery(st)
        md = rd.render_markdown(job_id="j1")
        assert "XSW 越权" in md and "信息泄露" in md
        assert "confirmed" in md  # 复现成功的显示 confirmed
        lines = md.splitlines()
        # 高严重度在前
        assert lines.index(
            next(x for x in lines if "XSW 越权" in x and x.startswith("## ["))
        ) < lines.index(next(x for x in lines if "信息泄露" in x and x.startswith("## [")))

    def test_json_and_md_zip(self, tmp_path):
        st = self._seed(tmp_path)
        rd = ReportDelivery(st)
        out = rd.generate(job_id="j1", fmt="md-zip", out=tmp_path / "r.zip")
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "report.md" in names and "manifest.json" in names
            fids = [n for n in names if n.endswith("finding.json")]
            assert len(fids) == 2

    def test_evidence_consistency_stale(self, tmp_path):
        st = self._seed(tmp_path)
        rd = ReportDelivery(st)
        assert rd.stale_findings() == []
        out = rd.generate(job_id="j1", fmt="md", out=tmp_path / "r.md")
        assert out.is_file()
        assert rd.stale_findings() == []
        # 证据变更 → 报告过期（一致性不变量）
        f = st.list(status="confirmed")[0]
        f.add_evidence(EvidenceRef(kind="traffic", ref="snap-1", role="proof"))
        st.save(f)
        stale = rd.stale_findings()
        assert len(stale) == 1 and stale[0].id == f.id
        # 重新生成报告 → 不再过期
        rd.generate(job_id="j1", fmt="md", out=tmp_path / "r2.md")
        assert rd.stale_findings() == []

    def test_nine_state_triage(self, tmp_path):
        st = self._seed(tmp_path)
        f = st.list(status="confirmed")[0]
        f2 = st.transition(f.id, "resolved")
        assert f2.status == "resolved"
        f3 = st.transition(f.id, "confirmed")  # 回归
        assert f3.status == "confirmed"
        f4 = st.transition(f.id, "risk_accepted")
        assert f4.status == "risk_accepted"
        with pytest.raises(ValueError):
            st.transition(f.id, "inferred")  # 非法迁移
