"""I：TUI 数据层（无 textual 依赖部分）+ textual App 构建冒烟。"""

from __future__ import annotations

import json

import pytest


def test_tui_data_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    from secpt.cli import tui_data
    from secpt.core.orchestrator.frontier import Frontier, frontier_db_path

    assert tui_data.resolve_base("") == ""  # 无任务 → 空
    fr = Frontier(frontier_db_path("tb1"))
    fr.add("测试意图", depth=0, role="exploit", asset_scope="http://t:1")
    assert tui_data.resolve_base("") == "tb1"  # 缺省取最近活跃
    assert tui_data.resolve_base("explicit") == "explicit"  # 显式优先
    rows = tui_data.load_intents("tb1")
    assert rows and rows[0]["title"] == "测试意图"
    assert tui_data.load_intents("") == []
    # 事件流增量：写一行 → 拉到 → 再拉为空
    ev = tui_data.events_path("tb1")
    ev.write_text(
        json.dumps(
            {
                "seq": 1,
                "ts": 1.0,
                "job_id": "tb1",
                "round": 0,
                "type": "status_change",
                "payload": {"status": "running"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _lines, off = tui_data.tail_events("tb1", 0)
    assert off > 0
    lines2, off2 = tui_data.tail_events("tb1", off)
    assert lines2 == [] and off2 == off
    assert tui_data.pending_rows() == []  # 无审批 → 空
    assert tui_data.decide("nope", True) is False


def test_tui_app_builds(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    from secpt.cli.tui_app import make_app

    app = make_app("")
    assert app.TITLE == "secpt 运维面板"
    assert app.base == ""
