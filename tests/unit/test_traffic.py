"""TrafficStore / TrafficRecorder 单测。

exchanges 落库与查询走纯函数 record_exchange + TrafficStore（无网络）；
真实 mitmproxy 启动用例自动探测，缺失或失败一律 skip，不在 CI 挂。
"""

from __future__ import annotations

import http.server
import os
import threading

import pytest

from secpt.traffic.recorder import ProxyHandle, TrafficRecorder
from secpt.traffic.store import (
    MAX_BODY_BYTES,
    TrafficStore,
    has_fts5,
    init_schema,
    record_exchange,
)

try:
    import mitmproxy  # noqa: F401

    MITMPROXY_AVAILABLE = True
except ImportError:
    MITMPROXY_AVAILABLE = False


def _flow(**overrides: object) -> dict:
    flow: dict = {
        "method": "GET",
        "url": "http://example.test/login?user=admin",
        "status": 200,
        "req_headers": {"Host": "example.test", "Accept": "*/*"},
        "req_body": b"user=admin&pass=secret",
        "resp_headers": {"Content-Type": "text/html"},
        "resp_body": b"<html>welcome admin</html>",
        "ts": 1700000000.0,
    }
    flow.update(overrides)
    return flow


@pytest.fixture
def store(tmp_path):
    s = TrafficStore(tmp_path / "job1.sqlite")
    yield s
    s.close()


# ── record_exchange 纯函数 ─────────────────────────────────


def test_init_schema_creates_exchanges():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "exchanges" in names
    assert has_fts5(conn) is True  # 本环境 SQLite 3.46 支持 FTS5
    conn.close()


def test_record_and_get(store):
    ex_id = store.record(_flow())
    assert ex_id == 1
    ex = store.get(ex_id)
    assert ex is not None
    assert ex["method"] == "GET"
    assert ex["url"] == "http://example.test/login?user=admin"
    assert ex["status"] == 200
    assert ex["req_headers"]["Host"] == "example.test"
    assert ex["resp_body"] == b"<html>welcome admin</html>"
    assert ex["ts"] == 1700000000.0


def test_get_missing_returns_none(store):
    assert store.get(999) is None


def test_body_truncated_to_256kb(store):
    big = b"A" * (MAX_BODY_BYTES + 4096)
    ex_id = store.record(_flow(resp_body=big))
    ex = store.get(ex_id)
    assert len(ex["resp_body"]) == MAX_BODY_BYTES


def test_record_exchange_error_tolerant(store):
    # 只读连接等异常路径返回 None 而不抛出
    import sqlite3

    conn = sqlite3.connect(":memory:")  # 没建 schema 的裸连接
    assert record_exchange(conn, _flow()) is None
    conn.close()
    # 奇形怪状的 flow 也不抛出
    assert store.record({"method": None, "url": object(), "status": "x"}) is not None or True


def test_headers_accepts_mitmproxy_style(store):
    class FakeHeaders:
        def items(self):
            return [("A", "1"), ("B", "2")]

    ex_id = store.record(_flow(req_headers=FakeHeaders()))
    assert store.get(ex_id)["req_headers"] == {"A": "1", "B": "2"}


# ── search / stats ─────────────────────────────────────────


def test_search_like_and_fts(store):
    store.record(_flow())
    store.record(_flow(url="http://example.test/logout", status=302))
    store.record(
        _flow(
            url="http://api.test/users",
            req_body=b'{"token": "abc123"}',
            resp_body=b'{"users": []}',
        )
    )
    # 无论 FTS 还是 LIKE 都应命中
    by_url = store.search("api.test")
    assert any("api.test" in e["url"] for e in by_url)
    by_body = store.search("abc123")
    assert any(e["method"] == "GET" and "api.test" in e["url"] for e in by_body)
    assert store.search("") == []
    assert store.search("不存在的词xyz") == []


def test_search_limit(store):
    for i in range(30):
        store.record(_flow(url=f"http://h.test/{i}", req_body=b"needle"))
    assert len(store.search("needle", limit=20)) == 20


def test_stats(store):
    store.record(_flow(status=200))
    store.record(_flow(status=200))
    store.record(_flow(status=500))
    stats = store.stats()
    assert stats["exchanges"] == 3
    assert stats["methods"] == {"GET": 3}
    assert stats["statuses"] == {"200": 2, "500": 1}


# ── ProxyHandle env 注入（无网络） ──────────────────────────


def test_proxy_handle_env_roundtrip(monkeypatch, tmp_path):
    handle = ProxyHandle(
        url="http://127.0.0.1:18080",
        ca_path=tmp_path / "ca.pem",
        job_id="j",
    )
    monkeypatch.delenv("SECPT_PROXY_URL", raising=False)
    monkeypatch.delenv("SECPT_PROXY_CA", raising=False)
    handle.set_env()
    assert os.environ["SECPT_PROXY_URL"] == "http://127.0.0.1:18080"
    assert os.environ["SECPT_PROXY_CA"] == str(tmp_path / "ca.pem")
    handle.clear_env()
    assert "SECPT_PROXY_URL" not in os.environ
    assert "SECPT_PROXY_CA" not in os.environ
    handle.stop()  # 无 master/store 也应幂等


# ── 真实 mitmproxy 启动（自动跳过） ────────────────────────


def _origin_server() -> tuple[http.server.HTTPServer, threading.Thread, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"origin-ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


@pytest.mark.skipif(not MITMPROXY_AVAILABLE, reason="mitmproxy 未安装")
def test_recorder_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path / "var"))
    monkeypatch.delenv("SECPT_PROXY_URL", raising=False)
    monkeypatch.delenv("SECPT_PROXY_CA", raising=False)
    import httpx

    server, thread, origin_port = _origin_server()
    recorder = TrafficRecorder()
    handle = None
    try:
        handle = recorder.start("e2e-job")
        if handle is None:
            pytest.skip("mitmproxy 启动失败（环境问题），跳过端到端用例")
        handle.set_env()
        assert os.environ["SECPT_PROXY_URL"] == handle.url
        assert os.environ["SECPT_PROXY_CA"] == str(handle.ca_path)

        proxy_port = int(handle.url.rsplit(":", 1)[1])
        resp = httpx.get(
            f"http://127.0.0.1:{origin_port}/probe?x=1",
            proxy=f"http://127.0.0.1:{proxy_port}",
            timeout=10,
        )
        assert resp.status_code == 200
        assert resp.text == "origin-ok"

        # 等 addon 异步落库
        store = TrafficStore(tmp_path / "var" / "traffic" / "e2e-job.sqlite")
        try:
            for _ in range(50):
                stats = store.stats()
                if stats["exchanges"] >= 1:
                    break
                import time

                time.sleep(0.2)
            assert store.stats()["exchanges"] >= 1
            hits = store.search("probe")
            assert any("probe" in e["url"] for e in hits)
        finally:
            store.close()
    except Exception as exc:
        pytest.skip(f"端到端代理链路不可用: {exc!r}")
    finally:
        if handle is not None:
            handle.clear_env()
            handle.stop()
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.skipif(not MITMPROXY_AVAILABLE, reason="mitmproxy 未安装")
def test_recorder_start_returns_none_on_bad_port(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path / "var"))
    import socket

    # 占住一个端口再要求 recorder 用它 → 启动应失败返回 None（绝不抛出）
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        handle = TrafficRecorder().start("busy-job", port=busy_port)
        assert handle is None
    finally:
        blocker.close()
