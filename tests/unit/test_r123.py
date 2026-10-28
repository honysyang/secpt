"""M3-4 判票票盒闭环测试：R1 只读重放 / SRS 经验判票 / Barrett 回放收紧。

对账蓝本 WF4（R1 仅只读、永不 rerun 写操作、裁判不 throw 只改账）与施工拍板：
- 拍板①：注入式 ReplayExecutor——judge 层只见 callable/轻量协议，不 import tools；
- 拍板②：R2 双路径/R3 反证 = 图纸位悬置（audit 记"M5 图纸位"，claimed 保持）；
- 拍板③：SRS 经验判票增减都接，锚点判同（raw_ref == finding.evidence_ref），
  减票地板 0 且不动 verify_state；
- 拍板④：fetch_replay 只收 verified（Barrett 式回放在判票接线后顺带生效）。
分组：registry 只读重放门 / experience 票盒写口 / r1 主逻辑 / sweep 接线 / e2e。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Protocol

import pytest

from secpt.errors import LedgerError
from secpt.judge import GroundTruth, JudgeProfile
from secpt.judge.r123 import r1
from secpt.judge.sweep import sweep
from secpt.store import experience, ledger
from secpt.store import migrate as migrate_store
from secpt.tools.registry import ToolManifest, ToolOutput, ToolRegistry

GOOD_TXT = "22/tcp open ssh  OpenSSH 9.2"


def _make_db() -> sqlite3.Connection:
    return migrate_store.connect(":memory:")


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """票盒/判票测试的独立内存库：自带管子不依赖 conftest，用完关连接防句柄泄漏。"""
    c = _make_db()
    yield c
    c.close()


def _profile(pattern: str = "8080/http-nothing") -> JudgeProfile:
    """R4 未命中的 profile：把裁决交给 R1（ground truth 故意不匹配）。"""
    return JudgeProfile(
        enabled=True,
        ground_truth=(GroundTruth(id="GT-X", pattern=pattern),),
        source="r123-test",
    )


def _claim(conn: sqlite3.Connection, evidence_ref: str, task_id: str | None = None) -> str:
    """铺一条 claimed finding，返回 id。"""
    if task_id is None:
        task_id = ledger.start_task(conn, "判票演示", goal="g").id
    return ledger.append_finding(conn, task_id, "22/tcp open ssh", "端口扫描结果", evidence_ref).id


def _ticket_on(
    conn: sqlite3.Connection, task_id: str, evidence_ref: str, *, tool: str = "nmap"
) -> str:
    """铺一张小票，raw_ref 对齐 finding.evidence_ref（锚点判同的原料）。"""
    run = ledger.start_run(conn, task_id)
    step = ledger.record_step(conn, task_id, run.id, 1, "{}")
    ledger.append_ticket(
        conn,
        task_id,
        run.id,
        step.id,
        tool,
        {"target": "10.0.0.5", "ports": "22"},
        0,
        "扫描完成",
        evidence_ref,
    )
    return ledger.latest_task(conn).id or task_id


def _exp(
    conn: sqlite3.Connection, task_id: str, anchor: str, *, env: str = "lab", state: str = "claimed"
) -> str:
    """铺一条经验条目，raw_ref 锚在指定证据上。"""
    row = experience.record_experience(
        conn, task_id, env, state, "22 端口经验", "nmap -p 22", anchor
    )
    assert row is not None
    return row.id


def _out(*, exit_code: int | None = 0, raw_ref: str = "x.txt", summary: str = "") -> ToolOutput:
    return ToolOutput(
        tool_name="nmap", args={}, exit_code=exit_code, summary=summary, raw_ref=raw_ref
    )


class SpyReplay:
    """注入式重放执行器的测试替身：记账被谁调用、照预置回执返回（可返回 None）。"""

    def __init__(self, outcome: ToolOutput | None) -> None:
        self._outcome = outcome
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def readonly_replay(self, name: str, args: Mapping[str, str]) -> ToolOutput | None:
        self.calls.append((name, dict(args)))
        return self._outcome


@pytest.fixture()
def evidence(tmp_path: Path) -> Path:
    """原证据文件（R0 已过：存在且非空）。"""
    path = tmp_path / "gold.txt"
    path.write_text(GOOD_TXT, encoding="utf-8")
    return path


@pytest.fixture()
def replay_raw(tmp_path: Path) -> Path:
    """重放产物文件：默认与原证据同文（重放一致）。"""
    return tmp_path / "rerun.txt"


# ---------- registry 只读重放门（拍板①：写操作永不进重放） ----------------------------


class TestRegistryReplayGate:
    """readonly_replay：未知/写操作返回 None 零执行；只读工具照 dispatch 全权过闸。"""

    def _registry_with_spy(self, *, read_only: bool) -> tuple[ToolRegistry, list[str]]:
        ran: list[str] = []

        class SpyTool:
            manifest = ToolManifest(
                name="probe_tool",
                kind="in-process",
                description="spy",
                params={"x": "str"},
                required=("x",),
                read_only=read_only,
            )

            def run(self, args: Mapping[str, str]) -> ToolOutput:
                ran.append(args["x"])
                return _out(summary="done")

        return ToolRegistry([SpyTool()]), ran  # type: ignore[list-item]

    def test_manifest_read_only_defaults_and_declarations(self) -> None:
        from secpt.tools.adapters import AgentRunTool, DeclareFindingTool, NmapTool

        assert ToolManifest(name="x").read_only is False
        assert NmapTool().manifest.read_only is True
        assert AgentRunTool().manifest.read_only is False
        assert DeclareFindingTool().manifest.read_only is False

    def test_read_only_tool_replays_via_dispatch(self, tmp_path: Path) -> None:
        nmap_bin = tmp_path / "fake_nmap.sh"
        nmap_bin.write_text('#!/bin/sh\necho "22/tcp open ssh"\n', encoding="utf-8")
        nmap_bin.chmod(0o755)  # subprocess 直跑探针：可执行位不补会落 E_TOOL_FAIL
        from secpt.tools.adapters import NmapTool

        registry = ToolRegistry([NmapTool(binary=str(nmap_bin), raw_dir=str(tmp_path / "raw"))])
        out = registry.readonly_replay("nmap", {"target": "10.0.0.5", "ports": "22"})
        assert out is not None and out.exit_code == 0 and out.raw_ref != ""
        assert Path(out.raw_ref).exists()

    def test_write_tool_replay_returns_none_without_run(self) -> None:
        registry, ran = self._registry_with_spy(read_only=False)
        assert registry.readonly_replay("probe_tool", {"x": "arg"}) is None
        assert ran == []  # 写操作零执行（永不 rerun 的机制化，不靠自觉）

    def test_unknown_tool_replay_returns_none(self) -> None:
        registry, _ = self._registry_with_spy(read_only=True)
        assert registry.readonly_replay("ghost", {}) is None

    def test_replay_passes_guard_fail_closed(self, tmp_path: Path) -> None:
        """Guard 同权（§3.6）：重放也过闸；HIGH 级无人闸 = fail-closed 拒回执。"""
        from secpt.tools.guard import GuardConfig
        from secpt.tools.registry import ToolRegistry as R

        registry, ran = self._registry_with_spy(read_only=True)
        gated = R(  # type: ignore[call-arg]
            [registry.get("probe_tool")],  # type: ignore[list-item]
            guard=GuardConfig(tiers={"probe_tool": "high"}, hitl_mode="off"),
        )
        out = gated.readonly_replay("probe_tool", {"x": "arg"})
        assert out is not None and out.exit_code is None and out.raw_ref == ""
        assert ran == []  # 被闸拦下：执行器没跑

    def test_replay_honors_schema_validation(self, tmp_path: Path) -> None:
        from secpt.tools.adapters import NmapTool

        registry = ToolRegistry([NmapTool(binary=str(tmp_path / "n"), raw_dir="/tmp")])
        out = registry.readonly_replay("nmap", {})  # 缺必填 target
        assert out is not None and out.exit_code is None
        assert "E_SCHEMA_TOOL" in out.summary


# ---------- experience 票盒写口（拍板③：锚点判同、增减都接、地板 0） ------------------


class TestExperienceVotes:
    """promote_claimed / decay_votes：判票写口收在 store，audit actor=judge。"""

    def test_promote_claimed_upgrades_and_counts(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "t", "g").id
        good_id = _exp(conn, task_id, "runs/gold.txt")
        other_id = _exp(conn, task_id, "runs/other.txt")
        verified_id = _exp(conn, task_id, "runs/v.txt", state="verified")
        promoted = experience.promote_claimed(conn, task_id, "lab", "runs/gold.txt")
        assert [r.id for r in promoted] == [good_id]
        assert promoted[0].verify_state == "verified"
        assert promoted[0].useful_count == 1
        row = conn.execute(
            "SELECT verify_state, useful_count FROM experience WHERE id = ?", (good_id,)
        ).fetchone()
        assert row["verify_state"] == "verified" and row["useful_count"] == 1
        untouched_other = conn.execute(
            "SELECT verify_state, useful_count FROM experience WHERE id = ?", (other_id,)
        ).fetchone()
        assert untouched_other["verify_state"] == "claimed" and untouched_other["useful_count"] == 0
        untouched_verified = conn.execute(
            "SELECT verify_state, useful_count FROM experience WHERE id = ?", (verified_id,)
        ).fetchone()
        assert untouched_verified["useful_count"] == 0  # 已 verified 不重复记票
        audits = conn.execute("SELECT what FROM audit_log WHERE actor = 'judge'").fetchall()
        assert any("判票" in str(r["what"]) for r in audits)

    def test_promote_respects_environment(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "t", "g").id
        _exp(conn, task_id, "runs/gold.txt", env="prod", state="claimed")
        assert experience.promote_claimed(conn, task_id, "lab", "runs/gold.txt") == []

    def test_decay_floors_at_zero_and_keeps_state(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "t", "g").id
        row = experience.record_experience(conn, task_id, "lab", "claimed", "s", "c", "runs/g.txt")
        assert row is not None
        conn.execute("UPDATE experience SET useful_count = 2 WHERE id = ?", (row.id,))
        conn.commit()
        # 实减两票：2→1→0，命中一条返回 1；verify_state 纹丝不动。
        assert experience.decay_votes(conn, task_id, "lab", "runs/g.txt") == 1
        assert experience.decay_votes(conn, task_id, "lab", "runs/g.txt") == 1
        fresh = conn.execute("SELECT useful_count, verify_state FROM experience").fetchone()
        assert fresh["useful_count"] == 0 and fresh["verify_state"] == "claimed"
        # 地板 0：票已空，SQL 内 MAX 再减也不变负（命中即返回，数值钉在 0）。
        assert experience.decay_votes(conn, task_id, "lab", "runs/g.txt") == 1
        assert conn.execute("SELECT useful_count FROM experience").fetchone()["useful_count"] == 0

    def test_decay_logs_judge_audit(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "t", "g").id
        experience.record_experience(conn, task_id, "lab", "verified", "s", "c", "runs/g.txt")
        assert experience.decay_votes(conn, task_id, "lab", "runs/g.txt") == 1
        what = conn.execute("SELECT what FROM audit_log WHERE actor = 'judge'").fetchone()["what"]
        assert "降票" in what or "−1" in what or "-1" in what

    def test_empty_anchor_is_pure_noop(self, conn: sqlite3.Connection) -> None:
        conn = _make_db()
        assert experience.promote_claimed(conn, "t", "lab", "  ") == []
        assert experience.decay_votes(conn, "t", "lab", "") == 0


# ---------- r1 主逻辑（tenet：没有重放结论就不动判、绝不 throw 破状态） ----------------


class TestR1:
    """R1 分支全收：无票悬置 / 写操作图纸位 / 重放失败 / 一致升票 / 不一降票。"""

    def test_no_ticket_evidence_suspends(self, evidence: Path) -> None:
        """小票锚点判同的原料缺席（如 obs 锚/手铺证明）→ 图纸位悬置，不裁决。"""
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        spy = SpyReplay(_out())
        assert r1(conn, ledger.get_finding(conn, fid), spy.readonly_replay, "lab") is None
        assert spy.calls == []  # 没票就别把执行器拉出来（钉工位纪律）
        assert ledger.get_finding(conn, fid).verify_state == "claimed"
        what = conn.execute("SELECT what FROM audit_log WHERE actor = 'judge'").fetchone()["what"]
        assert "悬置" in what

    def test_write_op_ticket_suspends_with_paper_slot(
        self, evidence: Path, replay_raw: Path
    ) -> None:
        """写操作票：replay 返回 None → R2/R3 图纸位悬置（拍板②，M5 接线）。"""
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        _ticket_on(conn, task_id, str(evidence), tool="declare_finding")
        spy = SpyReplay(None)
        assert r1(conn, ledger.get_finding(conn, fid), spy.readonly_replay, "lab") is None
        assert spy.calls == [("declare_finding", {"target": "10.0.0.5", "ports": "22"})]
        assert ledger.get_finding(conn, fid).verify_state == "claimed"
        what = conn.execute("SELECT what FROM audit_log WHERE actor = 'judge'").fetchone()["what"]
        assert "R2/R3" in what or "图纸位" in what

    def test_replay_failure_suspends(self, evidence: Path, replay_raw: Path) -> None:
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        _ticket_on(conn, task_id, str(evidence))
        for bad in (_out(exit_code=None), _out(exit_code=1), _out(raw_ref="")):
            spy = SpyReplay(bad)
            assert r1(conn, ledger.get_finding(conn, fid), spy.readonly_replay, "lab") is None
            assert ledger.get_finding(conn, fid).verify_state == "claimed"
            what = conn.execute(
                "SELECT what FROM audit_log WHERE actor = 'judge' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()["what"]
            assert "重放失败" in what

    def test_match_verifies_with_rerun_and_rewards(self, evidence: Path, replay_raw: Path) -> None:
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        _ticket_on(conn, task_id, str(evidence))
        exp_id = _exp(conn, task_id, str(evidence))
        replay_raw.write_text(GOOD_TXT, encoding="utf-8")
        spy = SpyReplay(_out(raw_ref=str(replay_raw), summary="一致"))
        row = r1(conn, ledger.get_finding(conn, fid), spy.readonly_replay, "lab")
        assert row is not None and row.verify_state == "verified" and row.method == "rerun"
        assert row.judged_at is not None
        exp = conn.execute(
            "SELECT verify_state, useful_count FROM experience WHERE id = ?", (exp_id,)
        ).fetchone()
        assert exp["verify_state"] == "verified" and exp["useful_count"] == 1
        last = conn.execute(
            "SELECT what FROM audit_log WHERE actor = 'judge' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["what"]
        assert "verified(rerun)" in last

    def test_mismatch_decays_and_suspends(self, evidence: Path, replay_raw: Path) -> None:
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        _ticket_on(conn, task_id, str(evidence))
        exp_id = _exp(conn, task_id, str(evidence))
        replay_raw.write_text("80/tcp closed", encoding="utf-8")
        spy = SpyReplay(_out(raw_ref=str(replay_raw)))
        assert r1(conn, ledger.get_finding(conn, fid), spy.readonly_replay, "lab") is None
        row = ledger.get_finding(conn, fid)
        assert row.verify_state == "claimed"  # 减票不动判——不一致只说明证据不稳
        exp = conn.execute(
            "SELECT verify_state, useful_count FROM experience WHERE id = ?", (exp_id,)
        ).fetchone()
        assert exp["useful_count"] == -1 or exp["useful_count"] == 0
        what = conn.execute(
            "SELECT what FROM audit_log WHERE actor = 'judge' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["what"]
        assert "不匹配" in what or "降票" in what

    def test_mismatch_without_environment_still_suspends(
        self, evidence: Path, replay_raw: Path
    ) -> None:
        """没装配经验键（environment=None）→ 裁决照走、票盒静默（不炸不编账）。"""
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        _ticket_on(conn, task_id, str(evidence))
        replay_raw.write_text("80/tcp closed", encoding="utf-8")
        syspy = SpyReplay(_out(raw_ref=str(replay_raw)))
        assert r1(conn, ledger.get_finding(conn, fid), syspy.readonly_replay, None) is None
        assert ledger.get_finding(conn, fid).verify_state == "claimed"

    def test_missing_replay_raw_file_suspends(self, evidence: Path) -> None:
        """重放回执给了指针但文件不存在（不诚实锚点）→ 悬置，绝不 verified。"""
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        _ticket_on(conn, task_id, str(evidence))
        spy = SpyReplay(_out(raw_ref=str(evidence.parent / "ghost.txt")))
        assert r1(conn, ledger.get_finding(conn, fid), spy.readonly_replay, "lab") is None
        assert ledger.get_finding(conn, fid).verify_state == "claimed"


# ---------- sweep 接线（R4 未命中 → R1 接手；无重放守恒 M2 老口径） --------------------


class TestSweepR1Handoff:
    def _setup(self, evidence: Path) -> tuple[sqlite3.Connection, str, str]:
        conn = _make_db()
        task_id = ledger.start_task(conn, "t", "g").id
        fid = _claim(conn, str(evidence), task_id)
        _ticket_on(conn, task_id, str(evidence))
        return conn, task_id, fid

    def test_sweep_with_replay_promotes_r4_miss(self, evidence: Path, replay_raw: Path) -> None:
        conn, task_id, fid = self._setup(evidence)
        _exp(conn, task_id, str(evidence))
        replay_raw.write_text(GOOD_TXT, encoding="utf-8")
        verdicts = sweep(
            conn,
            task_id,
            _profile(),
            replay=SpyReplay(_out(raw_ref=str(replay_raw))).readonly_replay,
            environment="lab",
        )
        assert [r.id for r in verdicts] == [fid]
        assert verdicts[0].verify_state == "verified" and verdicts[0].method == "rerun"

    def test_sweep_without_replay_keeps_m2_silence(self, evidence: Path) -> None:
        """拍板守恒：不注入重放 = R0 过 R4 不中悬置不审（204 原班不跳红的机位）。"""
        conn, task_id, fid = self._setup(evidence)
        assert sweep(conn, task_id, _profile()) == []
        assert ledger.get_finding(conn, fid).verify_state == "claimed"
        assert (
            conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'judge'").fetchone()[0] == 0
        )

    def test_sweep_r1_idempotent(self, evidence: Path, replay_raw: Path) -> None:
        conn, task_id, fid = self._setup(evidence)
        _exp(conn, task_id, str(evidence))
        replay_raw.write_text(GOOD_TXT, encoding="utf-8")
        kwargs = {
            "replay": SpyReplay(_out(raw_ref=str(replay_raw))).readonly_replay,
            "environment": "lab",
        }
        first = sweep(conn, task_id, _profile(), **kwargs)
        second = sweep(conn, task_id, _profile(), **kwargs)
        assert len(first) == 1 and second == []
        counts = conn.execute("SELECT useful_count FROM experience").fetchall()
        assert all(row["useful_count"] == 1 for row in counts)  # 记票只记一次

    def test_sweep_r4_hit_wins_before_r1(self, evidence: Path, replay_raw: Path) -> None:
        """oracle 命中优先：到不了 R1，票盒不记票（R4 是更硬的背书）。"""
        conn, task_id, fid = self._setup(evidence)
        _exp(conn, task_id, str(evidence))
        verdicts = sweep(
            conn,
            task_id,
            JudgeProfile(
                enabled=True,
                ground_truth=(GroundTruth(id="GT-Y", pattern="22/tcp open"),),
                source=None,
            ),
        )
        assert verdicts[0].method == "oracle"
        assert conn.execute("SELECT useful_count FROM experience").fetchone()["useful_count"] == 0


# ---------- Barrett 回放收紧（拍板④：fetch_replay 只收 verified） ---------------------


class TestBarrettReplayGate:
    def test_fetch_replay_verified_only(self) -> None:
        conn = _make_db()
        for seq, (env, state) in enumerate(
            [("lab", "verified"), ("lab", "claimed"), ("lab", "rejected"), ("prod", "verified")]
        ):
            task_id = ledger.start_task(conn, f"in{seq}", "g").id
            row = experience.record_experience(
                conn, task_id, env, state, f"条{seq}", "cmd", "runs/a.txt"
            )
            assert row is not None
        replay = experience.fetch_replay(conn, "lab")
        assert [r.verify_state for r in replay] == ["verified"]  # claimed 也不进回放位


# ---------- e2e：FakeLLM 全链——真 registry 重放 → verified(rerun) → 票盒 → Barrett ----


def test_e2e_r1_full_loop_closes_the_ticket_box(tmp_path: Path) -> None:
    """nmap 真执行落票 → claim 申报 → 无重放 sweep 悬置 → 种经验 → 带重放 sweep
    晋级 verified(rerun) + 经验 +1 → fetch_replay 只见 verified（票盒闭环）。"""
    from secpt.tools.adapters import DeclareFindingTool, NmapTool

    nmap_bin = tmp_path / "fake_nmap.sh"
    nmap_bin.write_text('#!/bin/sh\necho "22/tcp open ssh"\n', encoding="utf-8")
    nmap_bin.chmod(0o755)  # subprocess 直跑探针：可执行位不补会落 E_TOOL_FAIL
    registry = ToolRegistry(
        [NmapTool(binary=str(nmap_bin), raw_dir=str(tmp_path / "raw")), DeclareFindingTool()]
    )
    conn = _make_db()

    # 预置演示账本（模彷真 run 后现场）：nmap 真执行诚实落盘 → 小票锚在产物上，
    # claimed finding 与经验条目锚都在同一条证据（锚点判同）。
    first = registry.dispatch("nmap", {"target": "10.0.0.5", "ports": "22"})
    assert first.exit_code == 0 and first.raw_ref != ""
    task = ledger.start_task(conn, "票盒闭环演示 10.0.0.5", goal="确认 ssh 暴露面")
    run = ledger.start_run(conn, task.id)
    step = ledger.record_step(conn, task.id, run.id, 1, "{}")
    ticket = ledger.append_ticket(
        conn,
        task.id,
        run.id,
        step.id,
        "nmap",
        {"target": "10.0.0.5", "ports": "22"},
        first.exit_code,
        "扫描完成",
        first.raw_ref,
    )
    finding = ledger.append_finding(
        conn, task.id, "22/tcp open ssh on 10.0.0.5", "OpenSSH banner", ticket.raw_ref
    )
    _exp(conn, task.id, finding.evidence_ref)

    # 第一轮：不带重放（守恒 M2 老口径）——R4 未命悬置。
    assert sweep(conn, task.id, _profile()) == []
    assert ledger.get_finding(conn, finding.id).verify_state == "claimed"

    # 事后判票：注入 registry 只读重放 → 重放产物与原证据同文 → verified(rerun)。
    verdicts = sweep(
        conn,
        task.id,
        _profile(),
        replay=registry.readonly_replay,
        environment="lab",
    )
    assert [r.id for r in verdicts] == [finding.id]
    row = ledger.get_finding(conn, finding.id)
    assert row.verify_state == "verified" and row.method == "rerun"

    # 票盒闭环：经验条目 useful_count+1 且升 verified；Barrett 回放位只见 verified。
    exp_rows = conn.execute("SELECT verify_state, useful_count FROM experience").fetchall()
    assert all(r["verify_state"] == "verified" and r["useful_count"] == 1 for r in exp_rows)
    replay = experience.fetch_replay(conn, "lab")
    assert len(replay) == 1 and replay[0].raw_ref == finding.evidence_ref

    # 幂等：重放只记一次票；第二轮 sweep 轮空。
    again = sweep(conn, task.id, _profile(), replay=registry.readonly_replay, environment="lab")
    assert again == []
    audits_a, audits_b = (
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor = 'judge'").fetchone()[0],
        conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE actor = 'judge' AND what LIKE '%%判票%%'"
        ).fetchone()[0],
    )
    assert audits_a >= 2 and audits_b == 1  # 裁决一行 + 判票一行，幂等不重复


def test_e2e_write_op_never_rerun(tmp_path: Path) -> None:
    """失败注入（写操作防线）：declare_finding 票 + 注入 registry 重放——
    DeclareFindingTool.read_only=False → replay 返回 None → R1 never executes，
    claim 悬置，audit 记 M5 图纸位。
    """
    from secpt.tools.adapters import DeclareFindingTool

    registry = ToolRegistry([DeclareFindingTool()])
    conn = _make_db()
    task_id = ledger.start_task(conn, "t", "g").id
    ledger.start_run(conn, task_id)
    evidence = tmp_path / "evidence.txt"
    evidence.write_text(GOOD_TXT, encoding="utf-8")  # R0 放行的前提：文件存在且非空
    fid = _claim(conn, str(evidence), task_id)
    _ticket_on(conn, task_id, str(evidence), tool="declare_finding")
    out = sweep(conn, task_id, _profile(), replay=registry.readonly_replay, environment="lab")
    assert out == []
    assert ledger.get_finding(conn, fid).verify_state == "claimed"
    what = conn.execute(
        "SELECT what FROM audit_log WHERE actor = 'judge' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()["what"]
    assert "图纸位" in what or "R2/R3" in what


def test_barrier_schema_version_guard() -> None:
    """无实义守门（防误删 import 面）：ReplayExecutor 协议必须在 r123 命名空间。

    判据用元类：typing.Protocol 在运行时不接受 ``isinstance(cls, Protocol)``
    语义（会走实例 isinstance 报 TypeError/False），协议类本身是 Protocol
    元类的实例——按元类身份判定才是可靠断言。
    """
    from secpt.judge import r123 as r123_mod

    assert isinstance(r123_mod.ReplayExecutor, type(Protocol))
    assert getattr(r123_mod.ReplayExecutor, "_is_protocol", False) is True


def test_ledger_error_propagation_from_experience_vote() -> None:
    """库层不吞错：票盒写口对 sqlite 故障抛 LedgerError（用只读连接注入失败）。"""
    conn = _make_db()
    task_id = ledger.start_task(conn, "t", "g").id
    row = experience.record_experience(conn, task_id, "lab", "claimed", "s", "c", "runs/g.txt")
    assert row is not None
    conn.close()  # 已关闭连接：一切 sqlite 操作失败
    with pytest.raises(LedgerError):
        experience.promote_claimed(conn, task_id, "lab", "runs/g.txt")
    with pytest.raises(LedgerError):
        experience.decay_votes(conn, task_id, "lab", "runs/g.txt")
