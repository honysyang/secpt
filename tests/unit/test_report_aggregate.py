"""阶段三：报告聚合（aggregate）+ 团队模式端到端 report 触发的验收测试。

覆盖验收判据（计划文档阶段三 §2）：
- build_report 只聚合 confirmed 且属于本任务的 findings；inferred/false_positive 进否定记录；
  资产来自 intel 图谱
- render_markdown 含目标/侦察发现/漏洞与证据/PoC 复现/影响/建议六段
- write_report 落盘 jobs_dir/<job_id>/report.md
- run_frontier role_plan 端到端：exploit 有成果后 report 意图自动预置、执行并落盘，
  done_events 含 report 完成广播
"""

from __future__ import annotations

import asyncio

from secpt.core.job.fakellm import FakeLLM
from secpt.core.orchestrator.frontier import run_frontier
from secpt.findings.models import EvidenceRef, Finding
from secpt.findings.store import FindingStore
from secpt.report.aggregate import build_report, render_markdown, write_report


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk_finding(fid: str, title: str, *, status="confirmed", job_id="j1") -> Finding:
    return Finding(
        id=fid,
        title=title,
        status=status,
        severity="high",
        job_id=job_id,
        first_seen={"job_id": job_id, "step": 1, "ts": 0},
        evidence=[EvidenceRef(kind="step", ref=f"{job_id}:1", role="proof")],
    )


class TestBuildReport:
    def test_confirmed_only_and_job_filter(self, tmp_path):
        fs = FindingStore(tmp_path / "findings.sqlite")
        fs.save(_mk_finding("f1", "RCE", status="confirmed", job_id="base"))
        fs.save(_mk_finding("f2", "XSS 疑似", status="inferred", job_id="base"))
        fs.save(_mk_finding("f3", "其他任务漏洞", status="confirmed", job_id="other"))

        from secpt.intel.store import IntelStore

        st = IntelStore(tmp_path / "intel.sqlite")
        st.observe_asset("endpoint", "http://t:1")

        rep = build_report(job_id="base", objective="目标", findings_store=fs, intel_store=st)
        assert [f["id"] for f in rep["findings"]] == ["f1"]  # 只 confirmed 且本任务
        assert [n["id"] for n in rep["negatives"]] == ["f2"]  # 未确认进否定记录
        assert any(a["value"] == "http://t:1" for a in rep["assets"])  # intel 资产

    def test_exhausted_intents_as_negatives(self, tmp_path):
        from secpt.core.orchestrator.frontier import Frontier

        fr = Frontier(tmp_path / "frontier.sqlite")
        iid = fr.add("爆破登录口", role="exploit", asset_scope="http://t:1")
        fr.settle(iid, status="exhausted", fail_reason="首次失败")
        rep = build_report(job_id="b1", objective="o", frontier=fr)
        assert any(e.get("title") == "爆破登录口" for e in rep["exhausted"])

    def test_subtask_id_prefix_match(self, tmp_path):
        """P0 回归：团队模式 findings 挂在子任务 id（base-iXXXX）下，聚合按前缀匹配。"""
        fs = FindingStore(tmp_path / "findings.sqlite")
        fs.save(_mk_finding("f1", "RCE", status="confirmed", job_id="base-i2420"))
        rep = build_report(job_id="base", objective="o", findings_store=fs)
        assert [f["id"] for f in rep["findings"]] == ["f1"]


class TestRenderMarkdown:
    def test_sections_and_content(self, tmp_path):
        fs = FindingStore(tmp_path / "findings.sqlite")
        fs.save(_mk_finding("f1", "RCE", status="confirmed"))
        rep = build_report(job_id="j1", objective="客户目标", findings_store=fs)
        md = render_markdown(rep)
        for section in (
            "## 1. 目标",
            "## 2. 侦察发现",
            "## 3. 漏洞与证据",
            "## 4. PoC 复现步骤",
            "## 5. 影响",
            "## 6. 建议与否定记录",
        ):
            assert section in md
        assert "客户目标" in md
        assert "RCE" in md
        assert "[high]" in md


class TestWriteReport:
    def test_writes_to_jobs_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        md = render_markdown(build_report(job_id="b1", objective="o"))
        path = write_report(md, job_id="b1")
        assert path == tmp_path / "jobs" / "b1" / "report.md"
        assert path.is_file()
        assert "# 渗透测试报告" in path.read_text(encoding="utf-8")


class TestVerifyQueue:
    """P1 回归：verify 从"依赖 planner 自觉补产"改为确定性触发。"""

    def _setup(self, tmp_path):
        from secpt.core.orchestrator.frontier import Frontier

        fr = Frontier(tmp_path / "frontier.sqlite")
        eid = fr.add("利用 http://t:1", role="exploit", asset_scope="http://t:1")
        fr.settle(eid, status="done", result="FLAG{ok}")
        return fr

    def test_verify_queued_when_inferred_finding(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import _queue_verify_if_needed

        fs = FindingStore(tmp_path / "findings.sqlite")
        fs.save(_mk_finding("f1", "SQLi", status="inferred", job_id="base-i2420"))
        fr = self._setup(tmp_path)
        assert _queue_verify_if_needed(fr, "base") is True
        rows = fr.list_all(status="open")
        v = [r for r in rows if r["role"] == "verify"]
        assert len(v) == 1
        assert "inferred" in v[0]["goal"]  # 复验指令写明查未确认 finding
        # 幂等：存在过即不再派
        assert _queue_verify_if_needed(fr, "base") is False

    def test_no_verify_without_findings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import _queue_verify_if_needed

        fr = self._setup(tmp_path)
        assert _queue_verify_if_needed(fr, "base") is False

    def test_no_verify_without_done_exploit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import Frontier, _queue_verify_if_needed

        fs = FindingStore(tmp_path / "findings.sqlite")
        fs.save(_mk_finding("f1", "SQLi", status="inferred", job_id="base-i2420"))
        fr = Frontier(tmp_path / "frontier.sqlite")  # 无 done exploit
        assert _queue_verify_if_needed(fr, "base") is False


class TestTeamModeReportE2E:
    def test_report_intent_auto_queued_and_written(self, tmp_path, monkeypatch):
        """端到端：recon→exploit done → report 意图自动预置执行 → report.md 落盘。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        summary = _run(
            run_frontier(
                "评估 http://t:1 的安全状态",
                profile="pentest",
                base_job_id="tb1",
                llm=FakeLLM(n_steps=1),
                planner_llm=FakeLLM(n_steps=1),
                role_plan=True,
                default_assets=["http://t:1"],
                max_rounds=20,
                max_workers=2,
            )
        )
        assert summary["mode"] == "frontier"
        # 完成广播应含 report 条目（exploit 有成果后 report 角色自动触发）
        roles = {e["role"] for e in summary["done_events"]}
        assert "report" in roles, f"done_events={summary['done_events']}"
        # report.md 落盘 + 内容骨架
        rpath = tmp_path / "jobs" / "tb1" / "report.md"
        assert rpath.is_file()
        md = rpath.read_text(encoding="utf-8")
        assert "# 渗透测试报告" in md
        assert "## 3. 漏洞与证据" in md
