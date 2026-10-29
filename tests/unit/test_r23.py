"""M5 R2 双路径 / R3 反证裁判测试：独立路径判定、流量取证、sweep 管线接线。

口径（对应设计文档 L73-76）：
- R2 的"独立"必须是 不同工具 + 不同证据锚 + 对侧已 verified——两条未验证的
  相似说法不互证（防同一幻觉自我壮胆）；
- R3 只认流量锚（obs:<id>）且要求实质事件（meta 非空 + snapshot 非空）；
- 不合格/无对账物的 claim 一律保持 claimed（悬置不审），绝不虚绿。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from secpt.judge import JudgeProfile
from secpt.judge.r23 import r2, r3
from secpt.judge.sweep import sweep
from secpt.store import ledger, observations
from secpt.store import migrate as migrate_store
from secpt.tools.registry import ToolOutput, ToolRegistry


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    c = migrate_store.connect(":memory:")
    yield c
    c.close()


def _profile() -> JudgeProfile:
    """R4 无 ground truth：把裁决交给 R1/R2/R3（oracle 不抢戏）。"""
    return JudgeProfile(enabled=True, ground_truth=(), source=None)


def _evidence(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text(f"证据 {name}\n", encoding="utf-8")
    return str(path)


def _ticket(conn: sqlite3.Connection, task_id: str, anchor: str, *, tool: str) -> None:
    run = ledger.start_run(conn, task_id)
    step = ledger.record_step(conn, task_id, run.id, 1, "{}")
    ledger.append_ticket(
        conn, task_id, run.id, step.id, tool, {"target": "10.0.0.5"}, 0, "ok", anchor
    )


def _finding(
    conn: sqlite3.Connection,
    task_id: str,
    anchor: str,
    *,
    title: str = "22/tcp open ssh",
    state: str | None = None,
    method: str | None = None,
) -> str:
    if state is None:
        return ledger.append_finding(conn, task_id, title, "detail", anchor).id
    return ledger.append_finding(
        conn, task_id, title, "detail", anchor, verify_state=state, method=method
    ).id


class _NoneReplay:
    """写操作重放替身：永远返回 None（零执行）。"""

    def __call__(self, name: str, args: Mapping[str, str]) -> ToolOutput | None:
        return None


# ---------- R2 双路径 -----------------------------------------------------------


class TestR2:
    def test_dual_path_verifies_with_independent_verified_sibling(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        task_id = ledger.start_task(conn, "双路径演示", goal="g").id
        anchor_a = _evidence(tmp_path, "a.txt")
        anchor_b = _evidence(tmp_path, "b.txt")
        _ticket(conn, task_id, anchor_a, tool="nmap")
        _ticket(conn, task_id, anchor_b, tool="curl_probe")
        _finding(conn, task_id, anchor_a, state="verified", method="rerun")
        claim_id = _finding(conn, task_id, anchor_b)

        row = r2(conn, ledger.get_finding(conn, claim_id))
        assert row is not None
        assert row.verify_state == "verified" and row.method == "dual-path"
        what = conn.execute(
            "SELECT what FROM audit_log WHERE actor='judge' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["what"]
        assert "dual-path" in what

    def test_same_tool_is_not_an_independent_path(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        task_id = ledger.start_task(conn, "同工具演示", goal="g").id
        anchor_a = _evidence(tmp_path, "a.txt")
        anchor_b = _evidence(tmp_path, "b.txt")
        _ticket(conn, task_id, anchor_a, tool="nmap")
        _ticket(conn, task_id, anchor_b, tool="nmap")
        _finding(conn, task_id, anchor_a, state="verified", method="rerun")
        claim_id = _finding(conn, task_id, anchor_b)

        assert r2(conn, ledger.get_finding(conn, claim_id)) is None
        assert ledger.get_finding(conn, claim_id).verify_state == "claimed"

    def test_unverified_sibling_does_not_corroborate(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """两条都没验证过的相似说法不互证（防同一幻觉自我壮胆）。"""
        task_id = ledger.start_task(conn, "互证禁止", goal="g").id
        anchor_a = _evidence(tmp_path, "a.txt")
        anchor_b = _evidence(tmp_path, "b.txt")
        _ticket(conn, task_id, anchor_a, tool="nmap")
        _ticket(conn, task_id, anchor_b, tool="curl_probe")
        _finding(conn, task_id, anchor_a)  # 仍 claimed
        claim_id = _finding(conn, task_id, anchor_b)

        assert r2(conn, ledger.get_finding(conn, claim_id)) is None

    def test_same_anchor_is_not_independent(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        task_id = ledger.start_task(conn, "同锚演示", goal="g").id
        anchor = _evidence(tmp_path, "a.txt")
        _ticket(conn, task_id, anchor, tool="nmap")
        _finding(conn, task_id, anchor, state="verified", method="rerun")
        claim_id = _finding(conn, task_id, anchor)

        assert r2(conn, ledger.get_finding(conn, claim_id)) is None

    def test_traffic_anchor_is_left_to_r3(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "流量锚演示", goal="g").id
        claim_id = _finding(conn, task_id, "obs:whatever", title="22/tcp open ssh")
        assert r2(conn, ledger.get_finding(conn, claim_id)) is None


# ---------- R3 反证 -------------------------------------------------------------


class TestR3:
    def _traffic(self, conn: sqlite3.Connection, task_id: str, meta: dict[str, str]) -> str:
        run = ledger.start_run(conn, task_id)
        row = observations.append(conn, task_id, run.id, "http", meta, "/tmp/snap.har")
        return f"obs:{row.id}"

    def test_traffic_evidence_verifies_negative(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "反证演示", goal="g").id
        anchor = self._traffic(conn, task_id, {"method": "GET", "path": "/admin"})
        claim_id = _finding(conn, task_id, anchor, title="管理页未授权可访问")

        row = r3(conn, ledger.get_finding(conn, claim_id))
        assert row is not None
        assert row.verify_state == "verified" and row.method == "negative"
        what = conn.execute(
            "SELECT what FROM audit_log WHERE actor='judge' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["what"]
        assert "negative" in what and "取证" in what

    def test_empty_meta_is_not_substantive_traffic(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "空壳演示", goal="g").id
        anchor = self._traffic(conn, task_id, {})
        claim_id = _finding(conn, task_id, anchor, title="空壳流量")

        assert r3(conn, ledger.get_finding(conn, claim_id)) is None
        assert ledger.get_finding(conn, claim_id).verify_state == "claimed"

    def test_missing_observation_row_suspends(self, conn: sqlite3.Connection) -> None:
        task_id = ledger.start_task(conn, "幽灵流量", goal="g").id
        claim_id = _finding(conn, task_id, "obs:ghost", title="幽灵流量")
        assert r3(conn, ledger.get_finding(conn, claim_id)) is None

    def test_file_anchor_has_no_counter_target(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        task_id = ledger.start_task(conn, "文件锚演示", goal="g").id
        anchor = _evidence(tmp_path, "a.txt")
        _ticket(conn, task_id, anchor, tool="curl_probe")  # 写操作票
        claim_id = _finding(conn, task_id, anchor, title="写操作结论")
        assert r3(conn, ledger.get_finding(conn, claim_id)) is None


# ---------- sweep 管线接线 ------------------------------------------------------


class TestSweepPipeline:
    def test_obs_claim_verified_by_r3_without_replay(self, conn: sqlite3.Connection) -> None:
        """M3 遗留的流量锚悬案：不注入重放也能由 R3 收口（纯读账推理）。"""
        task_id = ledger.start_task(conn, "管线演示", goal="g").id
        run = ledger.start_run(conn, task_id)
        obs = observations.append(
            conn, task_id, run.id, "http", {"method": "POST", "path": "/login"}, "/tmp/s.har"
        )
        claim_id = _finding(conn, task_id, f"obs:{obs.id}", title="登录爆破成功")

        verdicts = sweep(conn, task_id, _profile())
        assert [row.id for row in verdicts] == [claim_id]
        assert verdicts[0].method == "negative"

    def test_write_claim_upgraded_by_r2_after_r1_suspends(
        self, conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """写操作 claim：R1 悬置（永不 rerun）→ R2 见独立路径已 verified → dual-path。"""
        task_id = ledger.start_task(conn, "管线双路径", goal="g").id
        anchor_a = _evidence(tmp_path, "a.txt")
        anchor_b = _evidence(tmp_path, "b.txt")
        _ticket(conn, task_id, anchor_a, tool="nmap")
        _ticket(conn, task_id, anchor_b, tool="declare_finding")
        _finding(conn, task_id, anchor_a, state="verified", method="rerun")
        claim_id = _finding(conn, task_id, anchor_b)

        verdicts = sweep(conn, task_id, _profile(), replay=_NoneReplay(), environment="lab")
        assert [row.id for row in verdicts] == [claim_id]
        assert verdicts[0].method == "dual-path"

    def test_lone_write_claim_stays_claimed(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        """没有独立路径、没有流量：写完仍是 claimed——M5 不虚绿。"""
        task_id = ledger.start_task(conn, "孤证演示", goal="g").id
        anchor = _evidence(tmp_path, "a.txt")
        _ticket(conn, task_id, anchor, tool="declare_finding")
        claim_id = _finding(conn, task_id, anchor, title="孤证结论")

        assert sweep(conn, task_id, _profile(), replay=_NoneReplay()) == []
        assert ledger.get_finding(conn, claim_id).verify_state == "claimed"


def test_judge_exports_r2_r3() -> None:
    """导入面守门：R2/R3 必须从 judge 包一级导入（装配层只认这一层）。"""
    import secpt.judge as judge_pkg

    assert callable(judge_pkg.r2) and callable(judge_pkg.r3)


def test_registry_still_blocks_write_replay(tmp_path: Path) -> None:
    """R2/R3 上线不改"永不 rerun 写操作"：registry 只读门仍是唯一执行入口。"""
    from secpt.tools.adapters import DeclareFindingTool

    registry = ToolRegistry([DeclareFindingTool()])
    assert registry.readonly_replay("declare_finding", {"title": "t"}) is None
