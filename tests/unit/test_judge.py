"""W3-2 裁判 R0/R4 单测：profile 装配、判零锚点、oracle 命中、终态写口、sweep 收口、declare 正门。

与 e2e 分工：本文件聚焦裁判各器官的边界（拒/认/悬置/幂等/越权）；loop
刻点接线和 FakeLLM 裁判闭环留给 test_loop / test_e2e_fake_llm（验收①）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from secpt.errors import SecptError
from secpt.judge.profile import GroundTruth, JudgeProfile, load_profile
from secpt.judge.r0_self import r0
from secpt.judge.r4_oracle import r4
from secpt.judge.sweep import sweep
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import DeclareFindingTool
from secpt.tools.registry import ToolRegistry

GOOD_TXT = "22/tcp open ssh  OpenSSH 9.2"


def _make_db() -> sqlite3.Connection:
    return migrate_store.connect(":memory:")


def _cfg(tmp_path: Path, *, enabled: bool = True) -> Path:
    text = (
        "target = '10.0.0.5'\n"
        "[judge]\n"
        f"enabled = {str(enabled).lower()}\n"
        "[[ground_truth]]\n"
        'id = "GT-22"\n'
        'pattern = "22/tcp open"\n'
    )
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _row(
    *,
    title: str = "ssh 服务暴露",
    detail: str = "端口 22 开放",
    evidence_ref: str = "somewhere.txt",
) -> ledger.FindingRow:
    """R0/R4 fixture：手铺真 FindingRow（裁判只读 title/detail/evidence_ref）。"""
    return ledger.FindingRow(
        id="f_test",
        task_id="t_test",
        title=title,
        detail=detail,
        verify_state="claimed",
        method=None,
        evidence_ref=evidence_ref,
        confidence=None,
        judged_at=None,
        created_at=0,
    )


# ---------- profile 装配：合法 / 缺文件降关闭 / 坏 toml 拒跑 ---------------------------


class TestProfile:
    """judge.enabled 开关语义：缺 profile 不担任拒跑纪律（拍板③）。"""

    def test_valid_profile(self, tmp_path: Path) -> None:
        prof = load_profile(str(_cfg(tmp_path)))
        assert isinstance(prof, JudgeProfile)
        assert prof.enabled is True
        assert prof.source is not None and "profile.toml" in prof.source
        assert prof.ground_truth == (GroundTruth(id="GT-22", pattern="22/tcp open"),)
        assert prof.target == "10.0.0.5"

    def test_missing_file_degrades_to_disabled(self, tmp_path: Path) -> None:
        miss = tmp_path / "absent.toml"
        prof = load_profile(str(miss))
        assert prof.enabled is False
        assert prof.ground_truth == ()
        assert prof.source is None

        prof2 = load_profile(None)
        assert prof2.enabled is False and prof2.source is None

    def test_bad_toml_raises_e_profile(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("[judge\n= not valid ==", encoding="utf-8")
        with pytest.raises(SecptError, match="profile") as info:
            load_profile(str(bad))
        assert info.value.code == "E_PROFILE"


# ---------- 验收清单装配（M5 出场③） ------------------------------------------------


class TestAcceptanceProfile:
    """profile 的 [[acceptance]] 是"交付对目标"的机器判据（缺省=不判）。"""

    def test_parses_acceptance_items(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.toml"
        path.write_text(
            '[[acceptance]]\nid = "AC-flag"\npattern = "flag{"\ndescription = "拿到靶场 flag"\n',
            encoding="utf-8",
        )
        prof = load_profile(str(path))
        assert [(a.id, a.pattern) for a in prof.acceptance] == [("AC-flag", "flag{")]
        assert prof.acceptance[0].description == "拿到靶场 flag"

    def test_default_empty(self, tmp_path: Path) -> None:
        prof = load_profile(str(_cfg(tmp_path)))
        assert prof.acceptance == ()

    @pytest.mark.parametrize(
        "body",
        [
            "acceptance = 5\n",
            '[[acceptance]]\nid = "AC"\n',
            '[[acceptance]]\npattern = "x"\n',
        ],
    )
    def test_bad_acceptance_rejects(self, tmp_path: Path, body: str) -> None:
        path = tmp_path / "profile.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(SecptError) as info:
            load_profile(str(path))
        assert info.value.code == "E_PROFILE"


# ---------- R0 判零：锚点文件存在且非空（拍板②） --------------------------------------


class TestR0:
    """判零纯代码：空白锚 / 缺文件 / 零字节一律 reject，存在非空才放行。"""

    def test_good_anchor_passes(self, tmp_path: Path) -> None:
        good = tmp_path / "good.txt"
        good.write_text(GOOD_TXT, encoding="utf-8")
        assert r0(_make_db(), _row(evidence_ref=str(good))) is True

    def test_blank_ref_fails(self) -> None:
        conn = _make_db()
        assert r0(conn, _row(evidence_ref="")) is False
        assert r0(conn, _row(evidence_ref="   ")) is False

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        assert r0(_make_db(), _row(evidence_ref=str(tmp_path / "no-such-file.txt"))) is False

    def test_empty_file_fails(self, tmp_path: Path) -> None:
        zero = tmp_path / "zero.txt"
        zero.write_text("", encoding="utf-8")
        assert r0(_make_db(), _row(evidence_ref=str(zero))) is False


# ---------- R4 oracle：title/detail 不分大小写子串（拍板④） ---------------------------


class TestR4:
    """ground_truth 命中 = verified(oracle) 背书；未命中不裁决（R1-R3 是 M3 的事）。"""

    def _profile(self, pattern: str, *, enabled: bool = True) -> JudgeProfile:
        return JudgeProfile(
            enabled=enabled,
            ground_truth=(GroundTruth(id="GT-22", pattern=pattern),),
            source=None,
        )

    def test_match_case_insensitive(self) -> None:
        """大小写不敏感：pattern 小写也命中 '22/TCP OPEN'（拍板④）。"""
        hit = r4(_row(title="22/TCP OPEN"), self._profile("22/tcp open"))
        assert hit is not None and hit.id == "GT-22"

    def test_match_detail(self) -> None:
        assert r4(_row(detail="见 22/tcp open 输出"), self._profile("22/tcp open")) is not None

    def test_no_match_returns_none(self) -> None:
        assert r4(_row(), self._profile("8080/http")) is None

    def test_disabled_returns_none(self) -> None:
        """judge.enabled=false 时 r4 恒不裁决——有人误调也要让位。"""
        assert r4(_row(title="22/tcp open"), self._profile("22/tcp open", enabled=False)) is None


class TestMatchFirst:
    """pattern 多命中取首条清单序（拍板④）：整理 GT 一样要守序。"""

    def test_first_hit_wins(self) -> None:
        row = _row(title="22/tcp open", detail="")
        prof = JudgeProfile(
            enabled=True,
            ground_truth=(
                GroundTruth(id="GT-A", pattern="22/tcp open"),
                GroundTruth(id="GT-B", pattern="22/tcp"),
            ),
            source=None,
        )
        hit = r4(row, prof)
        assert hit is not None and hit.id == "GT-A"


# ---------- 状态机称写口：reg-harness 越权边界 / terminal 幂等区 -----------------------


class TestLedgerStateBoundary:
    """W3-2 复用 ledger.set_finding_state（专门 UPDATE 天门），不动 ledger API——
    terminal 拒绝即幂等天然（sweep 只取 claimed 也兜一层）。"""

    def test_claim_to_verified_oracle(self) -> None:
        conn = _make_db()
        task = ledger.start_task(conn, "锚点正", goal="harness 10.0.0.5")
        row = ledger.append_finding(conn, task.id, "f1", "d", "somewhere")
        out = ledger.set_finding_state(conn, row.id, "verified", method="oracle")
        assert out.verify_state == "verified" and out.method == "oracle"
        assert out.judged_at is not None

    def test_terminal_locked(self) -> None:  # 误判边界：终态后再次推进必须拒绝
        conn = _make_db()
        task = ledger.start_task(conn, "already scored", goal="g")
        row = ledger.append_finding(conn, task.id, "f", "d", "")
        ledger.set_finding_state(conn, row.id, "rejected")
        with pytest.raises(SecptError, match="终态"):
            ledger.set_finding_state(conn, row.id, "verified", method="oracle")


# ---------- sweep 集成：分类判 + 审计 + 幂等 -------------------------------------------


class TestSweep:
    """集合裁判：R0 拒 / oracle 认 / 未中保悬置；audit actor=judge（actor 五人之一）。"""

    def _setup_claims(self, conn: sqlite3.Connection, tmp_path: Path) -> tuple[str, str, str, str]:
        """三 claiming fixture：空锚（bad）/ 锚正面（gold）/ 锚在但不命中（unhit）。"""
        task = ledger.start_task(conn, "三种 claim", goal="g")
        gold = tmp_path / "gold.txt"
        gold.write_text(GOOD_TXT, encoding="utf-8")
        bad_id = ledger.append_finding(conn, task.id, "瞎说", "空锚", "").id
        gold_id = ledger.append_finding(conn, task.id, "22/tcp open 服务", "有真凭", str(gold)).id
        unhit_id = ledger.append_finding(conn, task.id, "没兜住的", "锚在但密码不对", str(gold)).id
        return task.id, bad_id, gold_id, unhit_id

    def _profile(self) -> JudgeProfile:
        return JudgeProfile(
            enabled=True,
            ground_truth=(GroundTruth(id="GT-1", pattern="22/tcp open"),),
            source=None,
        )

    def test_classify_and_audit(self, tmp_path: Path) -> None:
        conn = _make_db()
        task_id, bad_id, gold_id, unhit_id = self._setup_claims(conn, tmp_path)

        verdicts = sweep(conn, task_id, self._profile())
        states = {row.id: row.verify_state for row in verdicts}
        assert states[bad_id] == "rejected"
        assert states[gold_id] == "verified"
        assert unhit_id not in states  # R0 过 R4 不中 = 悬置 M3（不落裁决）

        bad = ledger.get_finding(conn, bad_id)
        gold = ledger.get_finding(conn, gold_id)
        assert bad.method is None  # rejected 不带 method（无依据不编）
        assert gold.method == "oracle" and gold.judged_at is not None
        audits = conn.execute(
            "SELECT what FROM audit_log WHERE actor = 'judge' ORDER BY rowid"
        ).fetchall()
        assert len(audits) == 2  # 两条被裁决的各审计一行；悬置的不审
        assert all("裁决" in str(r["what"]) for r in audits)

    def test_sweep_idempotent(self, tmp_path: Path) -> None:
        conn = _make_db()
        task_id, _bad, _gold, _unhit = self._setup_claims(conn, tmp_path)
        first = sweep(conn, task_id, self._profile())
        again = sweep(conn, task_id, self._profile())
        assert again == []  # 终态已锁，第二次轮空；悬置那条仍不命中
        assert len(first) == 2
        audits = conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'judge'").fetchone()[0]
        assert audits == 2  # 幂等不改审计

    def test_disabled_profile_is_noop(self, tmp_path: Path) -> None:
        conn = _make_db()
        task_id, *_ = self._setup_claims(conn, tmp_path)
        prof = JudgeProfile(enabled=False, ground_truth=(), source=None)
        assert sweep(conn, task_id, prof) == []
        audits = conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'judge'").fetchone()[0]
        assert audits == 0  # noop 连审计也不落


# ---------- declare_finding 工具：正门受理 / 空锚拒收 ----------------------------------


class TestDeclareTool:
    """claim 入口内嵌工具（拍板①）：manifest 三必填；run 只署回执不写账。"""

    def test_manifest_is_complete(self) -> None:
        tool = DeclareFindingTool()
        assert tool.manifest.name == "declare_finding"
        assert tool.manifest.kind == "in-process"
        assert set(tool.manifest.params) == {"title", "detail", "evidence_ref"}
        assert tuple(tool.manifest.required) == ("title", "detail", "evidence_ref")

    def test_blank_ref_is_e_tool_fail(self) -> None:
        out = DeclareFindingTool().run({"title": "空", "detail": "没有", "evidence_ref": "  "})
        assert out.exit_code is None
        assert "E_TOOL_FAIL" in out.summary
        assert out.raw_ref == ""

    def test_valid_ref_accepted(self, tmp_path: Path) -> None:
        gold = tmp_path / "g.txt"
        gold.write_text(GOOD_TXT, encoding="utf-8")
        out = DeclareFindingTool().run(
            {"title": "ssh 暴露", "detail": "22 开放", "evidence_ref": str(gold)}
        )
        assert out.exit_code == 0
        assert out.raw_ref == str(gold)

    def test_registry_front_gate(self) -> None:
        """registry 正门：缺必填 / 未知参数 / 控制字符 —— schema 校验拦下，run 不执行。"""
        registry = ToolRegistry([DeclareFindingTool()])
        for bad in [
            {"title": "t"},  # 缺必填
            {"title": "t", "detail": "d", "evidence_ref": "e", "extra": "x"},  # 未知参数
            {"title": "t\x00", "detail": "d", "evidence_ref": "e"},  # 控制字符
        ]:
            out = registry.dispatch("declare_finding", bad)
            assert out.exit_code is None

        good = registry.dispatch(
            "declare_finding", {"title": "t", "detail": "d", "evidence_ref": "/tmp/x"}
        )
        assert good.exit_code == 0 and good.summary != ""
