"""findings 验证纪律测试：模型/状态机/上报工具/对抗式复现。"""

from __future__ import annotations

import asyncio

import pytest

from secpt.findings.models import EvidenceRef, Finding
from secpt.findings.store import FindingStore
from secpt.findings.tool import make_report_finding_fn, maybe_attach


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk_finding(**kw) -> Finding:
    return Finding(
        title="SAML XSW 越权",
        vuln_class="saml_xsw",
        severity="high",
        target="http://target.example/",
        description="签名包装绕过",
        reproduction="1. 抓登录包 2. 注入伪造断言 3. 重放",
        **kw,
    )


class TestStore:
    def test_crud(self, tmp_path):
        st = FindingStore(tmp_path / "f.db")
        f = st.save(_mk_finding())
        got = st.get(f.id)
        assert got is not None and got.title == "SAML XSW 越权"
        assert got.status == "inferred"  # 创建即疑似
        assert st.list(status="inferred")[0].id == f.id
        assert st.list(target="target.example")

    def test_inferred_cannot_jump_to_confirmed(self, tmp_path):
        """确认纪律：inferred→confirmed 必须经对抗式复现，手动盖章被拒。"""
        st = FindingStore(tmp_path / "f.db")
        f = st.save(_mk_finding())
        with pytest.raises(ValueError, match="对抗式复现"):
            st.transition(f.id, "confirmed")
        # 合法迁移
        f2 = st.transition(f.id, "false_positive")
        assert f2.status == "false_positive"
        # 误判翻案
        f3 = st.transition(f.id, "inferred")
        assert f3.status == "inferred"

    def test_record_reproduction_upgrades(self, tmp_path):
        st = FindingStore(tmp_path / "f.db")
        f = st.save(_mk_finding())
        f = st.record_reproduction(f.id, job_id="vjob1", verdict="reproduced")
        assert f.status == "confirmed"  # 唯一升级路径
        assert f.reproduced_by[-1]["verdict"] == "reproduced"
        # fixed 不自动降级 confirmed（人工裁决）
        f = st.record_reproduction(f.id, job_id="vjob2", verdict="fixed")
        assert f.status == "confirmed"

    def test_evidence_dedup_and_version(self):
        f = _mk_finding()
        v0 = f.evidence_version
        f.add_evidence(EvidenceRef(kind="step", ref="j:1", role="proof"))
        f.add_evidence(EvidenceRef(kind="step", ref="j:1", role="proof"))  # 重复
        assert len(f.evidence) == 1
        assert f.evidence_version == v0 + 1


class TestReportFindingTool:
    def _fn(self, tmp_path):
        st = FindingStore(tmp_path / "f.db")
        box = {"n": 7}
        return make_report_finding_fn(st, job_id="j1", current_step=lambda: box["n"]), st

    def test_valid_report_creates_inferred(self, tmp_path):
        fn, st = self._fn(tmp_path)
        out = _run(
            fn(
                {
                    "title": "RCE",
                    "target": "http://t/",
                    "severity": "critical",
                    "description": "d",
                    "reproduction": "steps",
                    "evidence_quote": "uid=0(root)",
                }
            )
        )
        assert out.startswith("OK: 已登记")
        f = st.list()[0]
        assert f.status == "inferred"
        assert any(e.kind == "step" and e.ref == "j1:7" for e in f.evidence)
        assert any(e.kind == "quote" for e in f.evidence)

    def test_missing_required_rejected(self, tmp_path):
        fn, st = self._fn(tmp_path)
        out = _run(fn({"title": "x", "target": "http://t/"}))  # 无 reproduction
        assert out.startswith("Error: 缺少必填字段")
        assert st.list() == []

    def test_bad_severity_rejected(self, tmp_path):
        fn, _st = self._fn(tmp_path)
        out = _run(
            fn(
                {
                    "title": "x",
                    "target": "t",
                    "description": "d",
                    "reproduction": "r",
                    "severity": "super",
                }
            )
        )
        assert "severity" in out

    def test_duplicate_merged(self, tmp_path):
        fn, _st = self._fn(tmp_path)
        args = {"title": "XSS", "target": "http://t/", "description": "d", "reproduction": "r"}
        assert _run(fn(args)).startswith("OK: 已登记")
        assert _run(fn(args)).startswith("OK(duplicate)")

    def test_maybe_attach_only_when_declared(self):
        tools, _box = maybe_attach({}, ["bash"], "j1")
        assert "report_finding" not in tools
        tools, _box = maybe_attach({}, ["report_finding"], "j1")
        assert "report_finding" in tools


class TestVerifyFlow:
    def test_verdict_from_answer(self, tmp_path, monkeypatch):
        """对抗式复现的裁决：ANSWER REPRODUCED → confirmed（mock 执行与评判）。"""
        from secpt.findings import verify as _v

        st = FindingStore(tmp_path / "f.db")
        f = st.save(_mk_finding())

        async def fake_run(f, *, adversarial, llm=None, model="m", job_id=None):
            return {"verdict": "reproduced", "job_id": "vjob-x", "answer": "REPRODUCED"}

        monkeypatch.setattr(_v, "_run_verification", fake_run)
        f2 = _run(_v.verify_finding(f.id, store=st))
        assert f2.status == "confirmed"
        assert f2.reproduced_by[-1]["job_id"] == "vjob-x"

    def test_not_reproduced_stays_inferred(self, tmp_path, monkeypatch):
        from secpt.findings import verify as _v

        st = FindingStore(tmp_path / "f.db")
        f = st.save(_mk_finding())

        async def fake_run(f, *, adversarial, llm=None, model="m", job_id=None):
            return {"verdict": "fixed", "job_id": "vjob-y", "answer": "NOT_REPRODUCED"}

        monkeypatch.setattr(_v, "_run_verification", fake_run)
        f2 = _run(_v.verify_finding(f.id, store=st))
        assert f2.status == "inferred"  # 复现失败不升级
        assert f2.reproduced_by[-1]["verdict"] == "fixed"
