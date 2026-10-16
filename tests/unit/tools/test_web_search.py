"""web_search 插件：三后端选择 / 结果格式 / 未配置指引 / 失败不抛。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[3] / "tools" / "plugins"
sys.path.insert(0, str(PLUGINS_DIR))


def _load():
    spec = importlib.util.spec_from_file_location(
        "secpt_plugin_web_search", PLUGINS_DIR / "web_search.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, data: dict):
        self._data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=None):
        return self._data


def _clear_env(monkeypatch):
    for k in ("TAVILY_API_KEY", "BRAVE_API_KEY", "SECPT_SEARCH_URL"):
        monkeypatch.delenv(k, raising=False)


def test_register_contract():
    from secpt.tools.plugin_loader import load_python_plugin

    tools = load_python_plugin(PLUGINS_DIR / "web_search.py")
    assert "web_search" in tools
    schema, fn = tools["web_search"]
    assert schema["type"] == "object" and schema["required"] == ["query"]
    assert callable(fn)


def test_no_backend_actionable_error(monkeypatch):
    b = _load()
    _clear_env(monkeypatch)
    out = asyncio.run(b.register()["web_search"][1]({"query": "x"}))
    assert out.startswith("Error:") and "TAVILY_API_KEY" in out and "SECPT_SEARCH_URL" in out


def test_tavily_backend(monkeypatch):
    b = _load()
    _clear_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tk")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp({"results": [{"title": "T1", "url": "http://a", "content": "snippet1"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = asyncio.run(b.register()["web_search"][1]({"query": "泛微 e-cology SQL注入"}))
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["body"]["api_key"] == "tk" and seen["body"]["max_results"] == 5
    assert "T1" in out and "http://a" in out and "snippet1" in out


def test_brave_backend(monkeypatch):
    b = _load()
    _clear_env(monkeypatch)
    monkeypatch.setenv("BRAVE_API_KEY", "bk")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(
            {"web": {"results": [{"title": "B1", "url": "http://b", "description": "bs"}]}}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = asyncio.run(b.register()["web_search"][1]({"query": "q", "max_results": 3}))
    assert "api.search.brave.com" in seen["url"] and "count=3" in seen["url"]
    assert "B1" in out


def test_searxng_backend(monkeypatch):
    b = _load()
    _clear_env(monkeypatch)
    monkeypatch.setenv("SECPT_SEARCH_URL", "https://searx.local")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp({"results": [{"title": "S1", "url": "http://s", "content": "ss"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = asyncio.run(b.register()["web_search"][1]({"query": "q"}))
    assert seen["url"].startswith("https://searx.local/search?") and "format=json" in seen["url"]
    assert "S1" in out


def test_backend_failure_soft(monkeypatch):
    b = _load()
    _clear_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tk")

    def _boom(req, timeout=None):
        raise OSError("net down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    out = asyncio.run(b.register()["web_search"][1]({"query": "q"}))
    assert out.startswith("Error:") and "tavily" in out


def test_empty_query_rejected(monkeypatch):
    b = _load()
    _clear_env(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "tk")
    out = asyncio.run(b.register()["web_search"][1]({"query": "   "}))
    assert out.startswith("Error:") and "query" in out
