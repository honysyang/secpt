"""M4-3 mitmproxy 抓包后端契约测试：真流量回路零外联。

测试策略（蓝本 §10：本地球场，不碰外网）：
- 失败注入（二进制缺失 / 端口永不就绪）用桩二进制——快、确定、无真依赖；
- 真流量回路用真 mitmdump（skipif 未装）+ 本地 ThreadingHTTPServer +
  urllib ProxyHandler 显式走 PROXY 端口——全程 127.0.0.1 回环。

契约要点（任务书 M4-3）：
- 装配期 pre-flight fail-closed：mitmdump 未装/不可执行 → E_PROBE 拒跑；
- 运行窗内 start 失败维持 M3 可降级器官契约（wrap_execution 告警降级）；
- CaptureEvent.meta 只带 method/url/status，``mock`` 键永不出现（真流量
  如实标注）；snapshot 是真文件（诚实锚点）。
"""

from __future__ import annotations

import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from secpt.errors import SecptError
from secpt.tools.probe import build_backend, wrap_execution
from secpt.tools.probe_mitm import MitmCapture
from secpt.tools.registry import ToolOutput

# .venv 内的 mitmdump 与测试解释器同前缀（uv pip install 装进同一环境）。
_REAL_MITMDUMP = Path(sys.executable).parent / "mitmdump"
requires_mitmdump = pytest.mark.skipif(
    not _REAL_MITMDUMP.is_file(), reason=".venv 未装 mitmdump（真流量回路用例跳过）"
)

_FLAG_BODY = b"secpt-mitm-ok"


class _FlagHandler(BaseHTTPRequestHandler):
    """本地靶 server：GET 任何路径回 200 + 固定 body（零外联的真流量源）。"""

    def do_GET(self) -> None:  # noqa: N802  http.server 命名约定
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(_FLAG_BODY)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return None  # 静默：测试输出只看断言


def _start_local_http() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FlagHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _stub_mitmdump(tmp_path: Path) -> str:
    """桩二进制：``--version`` 秒回 0（骗过 pre-flight），其余场景睡死不监听。

    用于"起不来"失败注入：装配期检查通过、运行窗端口永不就绪——恰好卡在
    start() 的就绪轮询上，验证可降级器官语义。
    """
    script = tmp_path / "stub-mitmdump.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('Mitmproxy stub (test)')\n"
        "    sys.exit(0)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


# ---------- 装配期 pre-flight（fail-closed） -----------------------------------------


class TestBuildBackendPreflight:
    def test_missing_binary_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """点名 mitmproxy 档而 mitmdump 未装 → E_PROBE 拒跑，绝不静默换 mock。"""
        monkeypatch.setenv("SECPT_MITMDUMP_BIN", "/nonexistent/mitmdump-ghost")
        with pytest.raises(SecptError) as excinfo:
            build_backend("mitmproxy", tmp_path)
        assert excinfo.value.code == "E_PROBE"
        assert "mitmdump" in str(excinfo.value)

    def test_stub_binary_assembles(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """pre-flight 通过（--version 可执行）→ 返回 MitmCapture（真装配，不装假）。"""
        monkeypatch.setenv("SECPT_MITMDUMP_BIN", _stub_mitmdump(tmp_path))
        backend = build_backend("mitmproxy", tmp_path)
        assert isinstance(backend, MitmCapture)


# ---------- 运行窗内失败（M3 可降级器官契约） ----------------------------------------


class TestStartFailure:
    def test_never_listening_times_out_and_stop_is_safe(self, tmp_path: Path) -> None:
        """端口永不就绪 → start 超时抛错；start 失败后 stop 幂等不炸、交割空。"""
        backend = MitmCapture(
            raw_dir=tmp_path, mitmdump_bin=_stub_mitmdump(tmp_path), ready_timeout_s=0.5
        )
        with pytest.raises(TimeoutError):
            backend.start()
        assert backend.stop() == []

    def test_wrap_execution_degrades(self, tmp_path: Path) -> None:
        """start 失败走 M3 契约：告警降级、执行零干扰、回执完好、事件空。"""
        outcome = ToolOutput(tool_name="x", args={}, exit_code=0, summary="执行过一次", raw_ref="")
        backend = MitmCapture(
            raw_dir=tmp_path, mitmdump_bin=_stub_mitmdump(tmp_path), ready_timeout_s=0.5
        )
        output, events = wrap_execution(backend, lambda: outcome)
        assert output.summary == "执行过一次"
        assert events == []


# ---------- stop 安全性 ---------------------------------------------------------------


def test_stop_without_start_returns_empty(tmp_path: Path) -> None:
    """stop 无 start（未开窗）= 空交割，幂等不炸。"""
    assert MitmCapture(raw_dir=tmp_path).stop() == []


# ---------- 真流量回路（真 mitmdump + 本地 server，skipif 未装） -----------------------


@requires_mitmdump
class TestRealCapture:
    def test_roundtrip(self, tmp_path: Path) -> None:
        """本地靶流量经 PROXY 端口 → stop 交割真事件：meta 无 mock 键、快照是真文件。"""
        server, target_port = _start_local_http()
        backend = MitmCapture(raw_dir=tmp_path, mitmdump_bin=str(_REAL_MITMDUMP))
        try:
            backend.start()
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{backend.proxy_port}"})
            )
            with opener.open(f"http://127.0.0.1:{target_port}/flag", timeout=10) as resp:
                assert resp.read() == _FLAG_BODY
        finally:
            server.shutdown()
            server.server_close()

        events = backend.stop()
        hits = [e for e in events if str(e.meta.get("url", "")).endswith("/flag")]
        assert len(hits) == 1
        event = hits[0]
        assert event.channel == "http"
        assert event.meta["method"] == "GET"
        assert event.meta["status"] == 200
        assert "mock" not in event.meta  # 真流量如实标注：mock 键永不出现
        snapshot = Path(event.snapshot_ref)
        assert snapshot.is_file() and snapshot.stat().st_size > 0
        assert snapshot.parent == tmp_path
        assert "secpt-mitm-ok" in snapshot.read_text(encoding="utf-8")

    def test_two_requests_yield_two_events(self, tmp_path: Path) -> None:
        """窗内两次请求 → 两条事件（观测窗跟着动作走，窗内不漏不计多）。"""
        server, target_port = _start_local_http()
        backend = MitmCapture(raw_dir=tmp_path, mitmdump_bin=str(_REAL_MITMDUMP))
        try:
            backend.start()
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{backend.proxy_port}"})
            )
            for _ in range(2):
                with opener.open(f"http://127.0.0.1:{target_port}/flag", timeout=10) as resp:
                    resp.read()
        finally:
            server.shutdown()
            server.server_close()

        events = backend.stop()
        hits = [e for e in events if str(e.meta.get("url", "")).endswith("/flag")]
        assert len(hits) == 2
        assert all("mock" not in e.meta for e in hits)
