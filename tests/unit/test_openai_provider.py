"""M4-1 contract test：openai 兼容 provider（本地假 HTTP server，零联网）。

覆盖面（任务书 M4-1 验收口径）：
- 正常路径：complete 返回 content；请求体（model/messages/temperature/stream=false）
  与 ``Authorization: Bearer`` 头逐字段断言；
- 超时：server 拖延 → TimeoutError 冒泡（协议层退避后 E_TIMEOUT_LLM）；
- 4xx/5xx 分层：401/403→E_LLM_AUTH、429→E_LLM_RATE_LIMIT、其余→E_LLM_HTTP，
  provider 层 fail-fast 不重试（重试纪律只归协议层）；
- 坏响应：非 JSON / 缺 choices / content 非字符串 → E_LLM_RESPONSE；
- 密钥卫生：异常 context 的响应体片段里密钥出现即脱敏；
- 离线 e2e：openai 档 + 假 server 跑完整 ``secpt run`` / ``secpt chat``，
  与 mock 出厂脚本同构——真 provider 档不联网也能验收。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from secpt import main as main_mod
from secpt.errors import LlmError
from secpt.llms.client import LlmClient, Message, StepOutput, ToolCall
from secpt.llms.providers.openai_compat import OpenAICompatProvider

_API_KEY = "sk-test-secret-0001"
_MODEL = "fake-model"
_MESSAGES = [Message(role="user", content="go")]

# 脚本项语义：str=200 正常体 | int=HTTP 错误码 | float=先拖该秒数再回 200
#            | (status, body)=指定状态码的原样响应体（坏响应/泄漏体注入用）
ScriptItem = str | int | float | tuple[int, str]


def _make_handler(server: _FakeOpenAIServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body: dict[str, Any] = (
                    json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                )
                item = server.next_item()
                server.record(body, self.headers.get("Authorization", ""))
                if isinstance(item, float):
                    time.sleep(item)
                    item = "（拖延后的迟到响应）"
                if isinstance(item, int):
                    self.send_error(item)
                    return
                if isinstance(item, tuple):
                    status, payload = item
                else:
                    status = 200
                    payload = json.dumps(
                        {"choices": [{"message": {"role": "assistant", "content": item}}]},
                        ensure_ascii=False,
                    )
                self._send_json(payload, status)
            except Exception:  # noqa: BLE001 — 测试桩：意外一律 500 兜底，不让桩线程哑死
                self.send_error(500)

        def _send_json(self, payload: str, status: int) -> None:
            data = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            """静默：假 server 不往测试输出里刷请求日志。"""

    return Handler


class _FakeOpenAIServer:
    """本地 openai 兼容假 server：按脚本吐 /v1/chat/completions 响应，零联网。

    请求侧逐单留痕（body + Authorization 头），供"请求体形状"与"密钥只进请求头"
    两个契约断言；脚本游标线程锁护住，provider 退避重试并发打进来也不串账。
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str] = []
        self._script: list[ScriptItem] = []
        self._cursor = 0
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def queue(self, item: ScriptItem) -> None:
        with self._lock:
            self._script.append(item)

    def next_item(self) -> ScriptItem:
        with self._lock:
            item = self._script[self._cursor]
            self._cursor += 1
            return item

    def record(self, body: dict[str, Any], auth: str) -> None:
        with self._lock:
            self.requests.append(body)
            self.auth_headers.append(auth)


@pytest.fixture
def fake_server() -> Iterator[_FakeOpenAIServer]:
    server = _FakeOpenAIServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _provider(server: _FakeOpenAIServer) -> OpenAICompatProvider:
    return OpenAICompatProvider(base_url=server.base_url, api_key=_API_KEY, model=_MODEL)


class TestHappyPath:
    def test_complete_returns_content_with_expected_request_shape(
        self, fake_server: _FakeOpenAIServer
    ) -> None:
        fake_server.queue("你好，世界")
        out = _provider(fake_server).complete(_MESSAGES, temperature=0.2, timeout_s=5.0)
        assert out == "你好，世界"
        assert fake_server.requests == [
            {
                "model": _MODEL,
                "messages": [{"role": "user", "content": "go"}],
                "temperature": 0.2,
                "stream": False,
            }
        ]
        assert fake_server.auth_headers == [f"Bearer {_API_KEY}"]

    def test_through_protocol_layer_structured_call(self, fake_server: _FakeOpenAIServer) -> None:
        fake_server.queue(json.dumps({"thought": "先扫端口", "tool_calls": [], "done": False}))
        out = LlmClient(_provider(fake_server)).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert out.thought == "先扫端口"
        assert len(fake_server.requests) == 1


class TestHttpErrors:
    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (401, "E_LLM_AUTH"),
            (403, "E_LLM_AUTH"),
            (429, "E_LLM_RATE_LIMIT"),
            (500, "E_LLM_HTTP"),
            (503, "E_LLM_HTTP"),
        ],
    )
    def test_http_status_maps_to_layered_error(
        self, fake_server: _FakeOpenAIServer, status: int, code: str
    ) -> None:
        fake_server.queue(status)
        with pytest.raises(LlmError) as info:
            _provider(fake_server).complete(_MESSAGES, temperature=0.2, timeout_s=5.0)
        assert info.value.code == code
        assert info.value.context["status"] == status

    def test_provider_does_not_retry_http_errors(self, fake_server: _FakeOpenAIServer) -> None:
        fake_server.queue(500)
        with pytest.raises(LlmError):
            _provider(fake_server).complete(_MESSAGES, temperature=0.2, timeout_s=5.0)
        assert len(fake_server.requests) == 1  # fail-fast：重试纪律只归协议层

    def test_error_body_snippet_redacts_api_key(self, fake_server: _FakeOpenAIServer) -> None:
        leaky = json.dumps({"error": f"bad key {_API_KEY}"})
        fake_server.queue((500, leaky))
        with pytest.raises(LlmError) as info:
            _provider(fake_server).complete(_MESSAGES, temperature=0.2, timeout_s=5.0)
        body = str(info.value.context["body"])
        assert _API_KEY not in body
        assert "***" in body

    def test_connection_refused_maps_to_e_llm_connect(self) -> None:
        provider = OpenAICompatProvider(base_url="http://127.0.0.1:1", api_key="k", model="m")
        with pytest.raises(LlmError) as info:
            provider.complete(_MESSAGES, temperature=0.2, timeout_s=2.0)
        assert info.value.code == "E_LLM_CONNECT"


class TestBadResponse:
    @pytest.mark.parametrize(
        "payload",
        [
            "这不是 JSON",
            json.dumps({"object": "chat.completion"}),
            json.dumps({"choices": []}),
            json.dumps({"choices": [{"message": {"content": 123}}]}),
        ],
    )
    def test_malformed_body_raises_e_llm_response(
        self, fake_server: _FakeOpenAIServer, payload: str
    ) -> None:
        fake_server.queue((200, payload))
        with pytest.raises(LlmError) as info:
            _provider(fake_server).complete(_MESSAGES, temperature=0.2, timeout_s=5.0)
        assert info.value.code == "E_LLM_RESPONSE"


class TestTimeout:
    def test_slow_server_raises_timeout_error(self, fake_server: _FakeOpenAIServer) -> None:
        fake_server.queue(0.5)
        with pytest.raises(TimeoutError):
            _provider(fake_server).complete(_MESSAGES, temperature=0.2, timeout_s=0.1)

    def test_protocol_layer_backs_off_then_e_timeout_llm(
        self, fake_server: _FakeOpenAIServer
    ) -> None:
        fake_server.queue(0.5)
        fake_server.queue(0.5)
        with pytest.raises(LlmError) as info:
            LlmClient(
                _provider(fake_server), max_timeout_retries=2, backoff_base_s=0.001
            ).structured(_MESSAGES, StepOutput, temperature=0.2, timeout_s=0.1)
        assert info.value.code == "E_TIMEOUT_LLM"
        assert len(fake_server.requests) == 2


class TestOfflineE2E:
    def test_openai_provider_full_run_offline(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_server: _FakeOpenAIServer,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """M4-1 验收①：openai 兼容档可跑完整 run——本地假 server，零联网零真钥。

        脚本与 mock 出厂脚本同构：进场三问 → 一步 nmap（bin 必缺 → E_TOOL_FAIL
        回执）→ 收工 → 报告摘要；蒸馏器无候选不烧 LLM，整程恰 4 次调用。
        """
        monkeypatch.setenv("SECPT_DB", str(tmp_path / "harness.db"))
        monkeypatch.setenv("SECPT_RAW_DIR", str(tmp_path / "raw"))
        monkeypatch.setenv("SECPT_NMAP_BIN", str(tmp_path / "no-such-nmap"))
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("SECPT_LLM_BASE_URL", fake_server.base_url)
        monkeypatch.setenv("SECPT_LLM_API_KEY", _API_KEY)
        monkeypatch.setenv("SECPT_LLM_MODEL", _MODEL)
        fake_server.queue(
            json.dumps(
                {
                    "target": "10.0.0.1",
                    "objective": "openai 档离线演示",
                    "success_criteria": "全流程走通",
                },
                ensure_ascii=False,
            )
        )
        fake_server.queue(
            StepOutput(
                thought="摸扫一圈（openai 档演示步）",
                tool_calls=[ToolCall(name="nmap", args={"target": "10.0.0.1", "ports": "22,80"})],
            ).model_dump_json()
        )
        fake_server.queue(StepOutput(thought="收工", tool_calls=[], done=True).model_dump_json())
        fake_server.queue(
            json.dumps({"summary": "openai 兼容档离线演示：全流程走通。"}, ensure_ascii=False)
        )

        rc = main_mod.main(["run", "--goal", "对内网 10.0.0.1 做端口摸底"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "【小票】nmap" in out
        assert "exit_kind=normal" in out
        assert len(fake_server.requests) == 4
        assert all(h == f"Bearer {_API_KEY}" for h in fake_server.auth_headers)

    def test_openai_provider_chat_offline(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_server: _FakeOpenAIServer,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("SECPT_DB", str(tmp_path / "harness.db"))
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("SECPT_LLM_BASE_URL", fake_server.base_url)
        monkeypatch.setenv("SECPT_LLM_API_KEY", _API_KEY)
        monkeypatch.setenv("SECPT_LLM_MODEL", _MODEL)
        fake_server.queue("（openai 档）离线直答")

        rc = main_mod.main(["chat", "你好"])

        assert rc == 0
        assert "离线直答" in capsys.readouterr().out
        assert fake_server.auth_headers == [f"Bearer {_API_KEY}"]
