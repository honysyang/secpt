"""W3-4 只读 web 面板测试（Flask test_client，不真起服务器）。

分组：
- 总览：index 列 task；
- 黑板页：task/run/小票/findings/双图/audit 六块齐全，报告块 verified-only；
- 只读红线：POST 一律 405（无写路径）、面板处事只开 ro 连接（缺库不建文件）；
- SSE：stream 首块含账本快照。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from secpt.ob.web_view import create_app
from secpt.store import graph, ledger, migrate


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[Path, str]:
    """预置一座"跑完收束"的账本：task + run + step + 小票 + 双 finding + 双图 + audit。"""
    db = tmp_path / "panel.db"
    c = migrate.connect(str(db))
    task = ledger.start_task(c, "客户目标 10.0.0.5", "渗透 10.0.0.5 找 flag")
    run = ledger.start_run(c, task.id, {"provider": "mock"})
    step = ledger.record_step(c, task.id, run.id, 1, "{}")
    ledger.append_ticket(
        c,
        task.id,
        run.id,
        step.id,
        "nmap",
        {"target": "10.0.0.5"},
        0,
        "nmap 摸扫回执",
        "/tmp/raw/nmap_1.txt",
    )
    f1 = ledger.append_finding(
        c,
        task.id,
        "443 端口 Apache 漏洞",
        "detail 443",
        "/tmp/raw/nmap_1.txt",
        verify_state="claimed",
    )
    ledger.set_finding_state(c, f1.id, "verified", method="oracle")
    ledger.append_finding(
        c,
        task.id,
        "猜错的 22 端口弱口令",
        "detail 22",
        "/tmp/raw/other.txt",
        verify_state="rejected",
    )
    ledger.audit(c, task.id, "main", "裁判装配：mock")
    ledger.audit(c, task.id, "llmc", "教练提案（只提案不裁决）：换打 443")
    graph.put_node(c, task.id, "asset", "10.0.0.5", {"port": "443"})
    graph.put_edge(c, task.id, "10.0.0.5", "443 端口 Apache 漏洞", "finding")
    ledger.end_run(c, run.id)
    ledger.end_task(c, task.id, "normal")
    c.close()
    return db, task.id


def _client(db: Path) -> FlaskClient:
    return create_app(db).test_client()


# ---- 总览面 --------------------------------------------------------------------


def test_index_lists_tasks(seeded: tuple[Path, str]) -> None:
    db, task_id = seeded
    html = _client(db).get("/").data.decode()
    assert task_id in html
    assert "客户目标 10.0.0.5" in html


# ---- 黑板页 --------------------------------------------------------------------


def test_task_page_blackboard_complete(seeded: tuple[Path, str]) -> None:
    """黑板六块齐：任务/小票/双图/audit 与 verified-only 报告块。"""
    db, task_id = seeded
    html = _client(db).get(f"/task/{task_id}").data.decode()
    assert "渗透 10.0.0.5 找 flag" in html  # 任务 goal
    assert "nmap 摸扫回执" in html  # 小票卡
    assert "verified" in html and "oracle" in html  # findings 状态机明细
    assert "<script" not in html.lower()  # 只读面板不带任何可写脚本


def test_task_page_report_verified_only(seeded: tuple[Path, str]) -> None:
    """报告块只收 verified（半场如实：rejected 只进明细不进报告）。"""
    db, task_id = seeded
    html = _client(db).get(f"/task/{task_id}").data.decode()
    assert "443 端口 Apache 漏洞" in html  # verified 进报告块
    report_seg = html.split("报告")[1] if "报告" in html else ""
    assert "猜错的 22 端口弱口令" not in report_seg  # rejected 不进报告块
    assert "猜错的 22 端口弱口令" in html  # 但明细表如实照录
    assert "release verdict" not in html  # 面板不伪造状态


def test_task_page_shows_graph(seeded: tuple[Path, str]) -> None:
    db, task_id = seeded
    html = _client(db).get(f"/task/{task_id}").data.decode()
    assert "节点 1" in html  # graph_summary 文案
    assert "10.0.0.5" in html


def test_task_page_shows_audit(seeded: tuple[Path, str]) -> None:
    db, task_id = seeded
    html = _client(db).get(f"/task/{task_id}").data.decode()
    assert "llmc" in html
    assert "教练提案（只提案不裁决）：换打 443" in html


def test_unknown_task_404(seeded: tuple[Path, str]) -> None:
    db, _ = seeded
    assert _client(db).get("/task/deadbeef").status_code == 404


# ---- 只读红线 ------------------------------------------------------------------


def test_post_is_405(seeded: tuple[Path, str]) -> None:
    """无写路径：POST 打到面板任何已知路由都吃 405。"""
    db, task_id = seeded
    client = _client(db)
    assert client.post("/").status_code == 405
    assert client.post(f"/task/{task_id}").status_code == 405


def test_missing_db_creates_no_file(tmp_path: Path) -> None:
    """缺库不背锅建空文件：ro 连接 + 直接 404，现场保持干净。"""
    missing = tmp_path / "ghost.db"
    client = _client(missing)
    assert client.get("/").status_code == 404
    assert not missing.exists()


# ---- SSE 事件流 ----------------------------------------------------------------


def test_stream_first_chunk_carries_snapshot(seeded: tuple[Path, str]) -> None:
    db, task_id = seeded
    resp = _client(db).get(f"/task/{task_id}/stream")
    assert resp.content_type.startswith("text/event-stream")
    first = next(iter(resp.response))
    text = first.decode()
    assert text.startswith("data: ")
    assert "教练提案" in text  # 快照含最新 audit


def test_stream_only_get(seeded: tuple[Path, str]) -> None:
    db, task_id = seeded
    assert _client(db).post(f"/task/{task_id}/stream").status_code == 405
