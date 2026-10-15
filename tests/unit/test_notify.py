"""F：通知通道（webhook / notify-send；未配置零开销、失败静默）。"""

from __future__ import annotations

import json

from secpt.runtime import notify as n


def test_notify_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SECPT_NOTIFY_WEBHOOK", raising=False)
    monkeypatch.delenv("SECPT_NOTIFY_DESKTOP", raising=False)
    called = []
    monkeypatch.setattr(n, "_send", lambda *a: called.append(a))
    n.notify("e", "t")
    assert called == [] and n.configured() is False


def test_send_webhook_payload(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["data"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    n._send("flag_found", "t1", "FLAG{x}", "http://hook.invalid/x", False)
    assert seen["url"] == "http://hook.invalid/x"
    assert seen["data"]["event"] == "flag_found" and seen["data"]["title"] == "t1"


def test_send_desktop_notify(monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **k: calls.append(cmd))
    n._send("e", "标题", "正文", "", True)
    assert calls and calls[0][0] == "notify-send" and "标题" in calls[0]


def test_send_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise OSError("net down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    n._send("e", "t", "", "http://x", False)  # 静默，不抛
