"""轻量 Prometheus 指标测试：计数/仪表/直方图渲染 + 文本格式契约 + HTTP 端点。"""

from __future__ import annotations

import urllib.request

from secpt.runtime.metrics import (
    counter_inc,
    gauge_set,
    histogram_observe,
    render_exposition,
    start_metrics_server,
)


def test_counter_and_gauge_render():
    counter_inc("m_test_calls_total", model="a", status="ok")
    counter_inc("m_test_calls_total", model="a", status="ok")
    counter_inc("m_test_calls_total", model="b", status="error", value=2.5)
    gauge_set("m_test_active", 3)
    out = render_exposition()
    assert "# TYPE m_test_calls_total counter" in out
    assert 'm_test_calls_total{model="a",status="ok"} 2.0' in out
    assert 'm_test_calls_total{model="b",status="error"} 2.5' in out
    assert "m_test_active 3.0" in out


def test_histogram_render_contract():
    for v in (0.05, 0.3, 3.0):
        histogram_observe("m_test_latency_seconds", v, model="x")
    out = render_exposition()
    assert "# TYPE m_test_latency_seconds histogram" in out
    # Prometheus 契约：bucket 带原标签集 + le（含 +Inf）；_count/_sum 同标签
    assert 'm_test_latency_seconds_bucket{le="0.1",model="x"} 1' in out
    assert 'm_test_latency_seconds_bucket{le="0.5",model="x"} 2' in out
    assert 'm_test_latency_seconds_bucket{le="+Inf",model="x"} 3' in out
    assert 'm_test_latency_seconds_count{model="x"} 3' in out
    assert 'm_test_latency_seconds_sum{model="x"} 3.35' in out


def test_label_escaping():
    counter_inc("m_test_esc", note='a"b\\c\nd')
    out = render_exposition()
    assert 'note="a\\"b\\\\c\\nd"' in out


def test_metrics_http_endpoint():
    srv = start_metrics_server(port=0, host="127.0.0.1")
    try:
        port = srv.server_address[1]
        counter_inc("m_http_probe")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            body = r.read().decode("utf-8")
            assert r.headers["Content-Type"].startswith("text/plain")
        assert "m_http_probe 1.0" in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.read() == b"ok"
    finally:
        srv.shutdown()
