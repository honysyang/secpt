"""轻量 Prometheus 指标（零依赖，纯标准库）。

prometheus_client 不在项目依赖里（铁律：禁止新依赖），这里实现其最小子集：
Counter / Gauge / Histogram + 文本暴露格式（text/plain; version=0.0.4），
HTTP 端点用 stdlib http.server 起守护线程。

用法：
    from secpt.runtime.metrics import counter_inc, histogram_observe, gauge_set

    counter_inc("llm_calls_total", model="deepseek-chat", status="ok")
    histogram_observe("llm_latency_seconds", 0.42, model="deepseek-chat")
    gauge_set("active_jobs", 3)

    secpt worker metrics --port 9100   # GET /metrics 输出文本格式

线程安全：全局 Lock（写入频率低，锁开销可忽略）。同名指标首次创建时确定
标签集合与 bucket；后续同 key 累加/观测。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

_LOCK = threading.Lock()
# name -> {"type":..., "help":..., "buckets":..., "samples": {labels_key: value}}
# samples value：counter/gauge 为 float；histogram 为 {"count","sum","cum":{le:cnt}}
_METRICS: dict[str, dict] = {}


def _labels_key(labels: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))


def _escape_label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(key: tuple) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in key)
    return "{" + inner + "}"


def _get(name: str, mtype: str, help_: str = "", buckets: tuple | None = None) -> dict:
    with _LOCK:
        m = _METRICS.get(name)
        if m is None:
            m = {
                "type": mtype,
                "help": help_ or name,
                "buckets": buckets or DEFAULT_BUCKETS,
                "samples": {},
            }
            _METRICS[name] = m
        return m


def counter_inc(name: str, value: float = 1.0, help_: str = "", **labels) -> None:
    """计数器累加（llm_calls_total / tool_calls_total / compaction_count ...）。"""
    m = _get(name, "counter", help_)
    with _LOCK:
        key = _labels_key(labels)
        m["samples"][key] = m["samples"].get(key, 0.0) + float(value)


def gauge_set(name: str, value: float, help_: str = "", **labels) -> None:
    """仪表盘置值（当前并发/在途数等）。"""
    m = _get(name, "gauge", help_)
    with _LOCK:
        m["samples"][_labels_key(labels)] = float(value)


def histogram_observe(name: str, value: float, help_: str = "", **labels) -> None:
    """直方图观测（延迟等）：累计 count/sum + 各 bucket 计数。"""
    m = _get(name, "histogram", help_)
    with _LOCK:
        key = _labels_key(labels)
        s = m["samples"].setdefault(
            key, {"count": 0, "sum": 0.0, "cum": {le: 0 for le in m["buckets"]}}
        )
        v = float(value)
        s["count"] += 1
        s["sum"] += v
        for le in m["buckets"]:
            if v <= le:
                s["cum"][le] += 1


def render_exposition() -> str:
    """渲染 Prometheus 文本暴露格式（按指标名排序，输出稳定可 diff）。"""
    with _LOCK:
        names = sorted(_METRICS)
        lines: list[str] = []
        for name in names:
            m = _METRICS[name]
            lines.append(f"# HELP {name} {m['help']}")
            lines.append(f"# TYPE {name} {m['type']}")
            if m["type"] == "histogram":
                for key in sorted(m["samples"]):
                    s = m["samples"][key]
                    base = _fmt_labels(key)
                    # Prometheus 约定：bucket 必须含 +Inf（累计 = count）
                    for le in [*m["buckets"], float("inf")]:
                        cnt = s["count"] if le == float("inf") else s["cum"][le]
                        le_labels = _fmt_labels_with(key, {"le": _fmt_le(le)})
                        lines.append(f"{name}_bucket{le_labels} {cnt}")
                    lines.append(f"{name}_count{base} {s['count']}")
                    lines.append(f"{name}_sum{base} {s['sum']}")
            else:
                for key in sorted(m["samples"]):
                    lines.append(f"{name}{_fmt_labels(key)} {m['samples'][key]}")
        return "\n".join(lines) + ("\n" if lines else "")


def _fmt_le(le) -> str:
    return "+Inf" if le == float("inf") else (f"{le:g}")


def _fmt_labels_with(key: tuple, extra: dict) -> str:
    merged = dict(key) | {k: str(v) for k, v in extra.items()}
    return _fmt_labels(_labels_key(merged))


def start_metrics_server(port: int = 9100, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """起 /metrics HTTP 端点（守护线程）。返回 server 对象（shutdown() 停止）。"""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802（stdlib 命名约定）
            if self.path in ("/metrics", "/"):
                body = render_exposition().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/healthz":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, *args):  # 静默访问日志（指标端点无需噪声）
            pass

    srv = ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="secpt-metrics").start()
    return srv
