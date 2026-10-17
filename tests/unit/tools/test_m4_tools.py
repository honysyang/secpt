"""M4 工具面测试：OOB 回调、结构化侦察解析、通用打法模板、按需工具映射。"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from secpt.core.orchestrator import benchmark as bm
from secpt.core.orchestrator.frontier import Intent, build_intent_objective
from secpt.learning import techniques as tech
from secpt.runtime.workspace_ctx import current_work_dir, reset_work_dir, set_work_dir
from secpt.tools.plugin_loader import discover_plugins, load_python_plugin

_PLUGINS = Path(__file__).resolve().parents[3] / "tools" / "plugins"
OOB = load_python_plugin(_PLUGINS / "oob.py")["oob"][1]
RECON = load_python_plugin(_PLUGINS / "recon_parse.py")["recon_parse"][1]


class TestOob:
    def test_start_request_check_stop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_OOB_HOST", "127.0.0.1")
        try:
            out = json.loads(OOB({"action": "start", "bind": "127.0.0.1", "port": 0, "ttl_s": 60}))
            assert out["ok"] and out["url_base"].startswith("http://127.0.0.1:")
            req = urllib.request.Request(
                out["url_base"] + "/probe?x=1",
                data=b"ssrf-proof",
                method="POST",
                headers={"X-Test": "1"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.read() == b"ok"
            # 给 handler 落事件一点时间（同一进程内通常已完成）
            for _ in range(20):
                got = json.loads(OOB({"action": "check"}))
                if got.get("total"):
                    break
                time.sleep(0.05)
            assert got["total"] == 1
            ev = got["events"][0]
            assert "/probe" in ev["path"] and ev["body"] == "ssrf-proof"
            assert ev["headers"].get("X-Test") == "1"
            assert (tmp_path / "oob").is_dir()
            stop = json.loads(OOB({"action": "stop"}))
            assert stop["stopped"] and stop["total"] == 1
        finally:
            OOB({"action": "stop"})

    def test_bad_action(self):
        assert "action" in OOB({"action": "nope"})


class TestReconParse:
    def test_nmap_xml_to_assets(self, tmp_path):
        tok = set_work_dir(tmp_path)
        try:
            xml = """<?xml version="1.0"?>
<nmaprun><host><status state="up"/>
<address addr="10.0.0.5" addrtype="ipv4"/>
<ports>
 <port protocol="tcp" portid="80"><state state="open"/>
   <service name="http" product="nginx" version="1.24"/></port>
 <port protocol="tcp" portid="22"><state state="closed"/></port>
</ports></host></nmaprun>"""
            (tmp_path / "scan.xml").write_text(xml, encoding="utf-8")
            out = json.loads(RECON({"path": "scan.xml"}))
            assert out["format"] == "nmap_xml" and out["assets_added"] == 1
            assets = json.loads((tmp_path / "assets.json").read_text(encoding="utf-8"))
            assert assets[0] == {
                "host": "10.0.0.5",
                "port": 80,
                "service": "http",
                "tech": "nginx 1.24",
            }
        finally:
            reset_work_dir(tok)

    def test_ffuf_and_gobuster_merge(self, tmp_path):
        tok = set_work_dir(tmp_path)
        try:
            ff = {
                "results": [
                    {"url": "http://10.0.0.5/admin", "status": 200, "length": 10, "words": 2},
                    {"url": "http://10.0.0.5/.git/config", "status": 403, "length": 0, "words": 0},
                ]
            }
            (tmp_path / "ffuf.json").write_text(json.dumps(ff), encoding="utf-8")
            out = json.loads(RECON({"path": "ffuf.json"}))
            assert out["endpoints_added"] == 2
            gb = "http://10.0.0.5/admin (Status: 200) [Size: 10]\n/api (Status: 401)\n"
            (tmp_path / "gb.txt").write_text(gb, encoding="utf-8")
            out2 = json.loads(RECON({"path": "gb.txt", "host": "10.0.0.5"}))
            assert out2["endpoints_added"] == 1  # /admin 已存在（同 host/port/path/status 去重）
            endpoints = json.loads((tmp_path / "endpoints.json").read_text(encoding="utf-8"))
            paths = {e["path"] for e in endpoints}
            assert "/admin" in paths and "/.git/config" in paths and "/api" in paths
            assets = json.loads((tmp_path / "assets.json").read_text(encoding="utf-8"))
            assert any(a["host"] == "10.0.0.5" and a["service"] == "http" for a in assets)
        finally:
            reset_work_dir(tok)

    def test_missing_file(self, tmp_path):
        tok = set_work_dir(tmp_path)
        try:
            out = RECON({"path": "nope.xml"})
            assert out.startswith("Error:")
        finally:
            reset_work_dir(tok)


class TestTechniques:
    def test_match_ssrf_and_ssti(self, monkeypatch):
        monkeypatch.delenv("SECPT_TECHNIQUES", raising=False)
        ssrf = tech.match_techniques("存在 SSRF，url= 参数可请求内网 metadata")
        assert "SSRF" in ssrf and "oob" in ssrf
        ssti = tech.match_techniques("登录页 reflect 模板，尝试 jinja {{7*7}}")
        assert "SSTI" in ssti
        assert tech.match_techniques("普通的一句话描述") == ""

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("SECPT_TECHNIQUES", "0")
        assert tech.match_techniques("SSRF 内网") == ""

    def test_injected_into_intent_objective(self, monkeypatch):
        monkeypatch.delenv("SECPT_TECHNIQUES", raising=False)
        it = Intent(
            id="i1",
            title="测试 SSRF",
            goal="探测 url= 参数是否可请求内网 metadata",
            done_criteria="确认 SSRF",
            priority=1,
            depth=0,
            status="running",
            attempts=1,
        )
        obj = build_intent_objective("总目标", it, [], base="b1")
        assert "通用打法参考" in obj and "SSRF" in obj


class TestToolMapping:
    def test_web_and_oob(self):
        web = bm.tools_for_challenge("某企业门户 Web 登录接口存在注入")
        assert "http_request" in web and "session" in web and "recon_parse" in web
        ssrf = bm.tools_for_challenge("Web 服务存在 SSRF，url 参数可请求内网")
        assert "oob" in ssrf

    def test_binary(self):
        t = bm.tools_for_challenge("从固件镜像提取 MCU 可执行文件并逆向")
        assert "analyze_binary" in t and "recon_parse" in t

    def test_browser_default_off(self, monkeypatch):
        monkeypatch.delenv("SECPT_BENCH_BROWSER", raising=False)
        assert "browser_navigate" not in bm.tools_for_challenge("Web 页面 JS 应用")

    def test_plugins_discoverable(self):
        _yaml_p, py_t = discover_plugins()
        assert "oob" in py_t and "recon_parse" in py_t
        assert "http_request" in py_t


def test_workspace_ctx_roundtrip(tmp_path):
    assert current_work_dir() != str(tmp_path)
    tok = set_work_dir(tmp_path)
    assert current_work_dir() == str(tmp_path)
    reset_work_dir(tok)
    assert current_work_dir() != str(tmp_path)
