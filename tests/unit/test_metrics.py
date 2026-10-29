"""M4-6 两根验收杆（先测试）：精度杆与复利杆的口径锁定。

任务书口径（M4-6）：新增只读统计——verified 阳性率（精度杆）与经验命中率
（复利杆），纯度量不自动调参，CLI/面板可查，口径写进 docstring。

口径在本文件钉死（改口径必须先改这里再改实现）：
- 精度杆 = verified / (verified + rejected)：findings 终态中判真的比例。
  claimed 未裁决不进分母（计入会虚低）；分母为 0 → None（无从统计不编数）。
- 复利杆 = SUM(useful_count) / COUNT(*)：experience 平均净票——useful_count
  是重放命中的净记账（+1 命中 / -1 失配地板 0），均值 > 0 即经验库在产生
  正向复利；空库 → None。
"""

from __future__ import annotations

import pathlib

import pytest

from secpt import main as main_mod
from secpt.store import experience as experience_store
from secpt.store import ledger, metrics, migrate


def _finding(conn, title: str, state: str):
    f = ledger.append_finding(conn, "t1", title, "d", "/tmp/raw/x.txt")
    if state != "claimed":  # append 默认 claimed；claimed→claimed 非法迁移
        ledger.set_finding_state(conn, f.id, state, method="oracle")
    return f


def _experience(conn, summary: str, useful: int) -> None:
    experience_store.record_experience(
        conn, "t1", "lab", "verified", summary, "nmap -p 22", "/tmp/raw/x.txt"
    )
    if useful:
        conn.execute("UPDATE experience SET useful_count = ? WHERE summary = ?", (useful, summary))
        conn.commit()


# ---- 精度杆（verified 阳性率） ----------------------------------------------------


def test_precision_bar_counts_final_states_only() -> None:
    """claimed 不进分母：2 verified + 1 rejected + 1 claimed → 2/3。"""
    conn = migrate.connect(":memory:")
    conn.execute(
        "INSERT INTO tasks (id, input_text, goal, mode, created_at)"
        " VALUES ('t1', 'in', 'g', 'pentest', 0)"
    )
    _finding(conn, "a", "verified")
    _finding(conn, "b", "verified")
    _finding(conn, "c", "rejected")
    _finding(conn, "d", "claimed")
    assert metrics.verified_precision(conn) == pytest.approx(2 / 3)


def test_precision_bar_full_verified_is_one() -> None:
    conn = migrate.connect(":memory:")
    conn.execute(
        "INSERT INTO tasks (id, input_text, goal, mode, created_at)"
        " VALUES ('t1', 'in', 'g', 'pentest', 0)"
    )
    _finding(conn, "a", "verified")
    assert metrics.verified_precision(conn) == pytest.approx(1.0)


def test_precision_bar_no_final_states_is_none() -> None:
    """分母 0（全 claimed / 空库）→ None：无从统计，如实不编数。"""
    conn = migrate.connect(":memory:")
    assert metrics.verified_precision(conn) is None
    conn.execute(
        "INSERT INTO tasks (id, input_text, goal, mode, created_at)"
        " VALUES ('t1', 'in', 'g', 'pentest', 0)"
    )
    _finding(conn, "a", "claimed")
    assert metrics.verified_precision(conn) is None


# ---- 复利杆（经验命中率） --------------------------------------------------------


def test_compound_bar_is_mean_net_votes() -> None:
    """票 3/1/0 三条 → SUM 4 / COUNT 3 = 4/3（净值口径，+1 与 -1 都在账）。"""
    conn = migrate.connect(":memory:")
    conn.execute(
        "INSERT INTO tasks (id, input_text, goal, mode, created_at)"
        " VALUES ('t1', 'in', 'g', 'pentest', 0)"
    )
    _experience(conn, "e1", 3)
    _experience(conn, "e2", 1)
    _experience(conn, "e3", 0)
    assert metrics.experience_hit_rate(conn) == pytest.approx(4 / 3)


def test_compound_bar_empty_library_is_none() -> None:
    conn = migrate.connect(":memory:")
    assert metrics.experience_hit_rate(conn) is None


# ---- 渲染与 CLI/面板可查 ----------------------------------------------------------


def test_render_metrics_mentions_both_bars() -> None:
    conn = migrate.connect(":memory:")
    conn.execute(
        "INSERT INTO tasks (id, input_text, goal, mode, created_at)"
        " VALUES ('t1', 'in', 'g', 'pentest', 0)"
    )
    _finding(conn, "a", "verified")
    _experience(conn, "e1", 2)
    text = metrics.render_metrics(conn)
    assert "精度杆" in text and "复利杆" in text
    assert "100.0%" in text  # 1/1 终态
    assert "2.00" in text  # 2 净票 / 1 条


def test_render_metrics_empty_library_honest() -> None:
    conn = migrate.connect(":memory:")
    text = metrics.render_metrics(conn)
    assert "精度杆" in text and "复利杆" in text
    assert "暂无" in text


def test_metrics_cli_command_prints_bars(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``secpt metrics`` 子命令：两杆一行可查（验收 6 的 CLI 面）。"""
    db = tmp_path / "m.db"
    monkeypatch.setenv("SECPT_DB", str(db))
    assert main_mod.main(["metrics"]) == 0
    out = capsys.readouterr().out
    assert "精度杆" in out and "复利杆" in out
