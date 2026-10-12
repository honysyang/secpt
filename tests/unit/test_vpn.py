"""VPN 自举测试：配置解析/status/up/down（mock 系统命令，不真的连 VPN）。"""

from __future__ import annotations

import subprocess

from secpt.runtime import vpn


class TestConfig:
    def test_defaults_none(self, monkeypatch):
        monkeypatch.delenv("SECPT_VPN_TYPE", raising=False)
        assert vpn.vpn_type() == "none"
        assert not vpn.vpn_configured()

    def test_openvpn_configured(self, tmp_path, monkeypatch):
        cfg = tmp_path / "client.ovpn"
        cfg.write_text("client\n", encoding="utf-8")
        monkeypatch.setenv("SECPT_VPN_TYPE", "openvpn")
        monkeypatch.setenv("SECPT_VPN_CONFIG", str(cfg))
        assert vpn.vpn_configured()


class TestStatusProbe:
    def test_no_interface_false(self, monkeypatch):
        monkeypatch.setattr(vpn, "_interface_up", lambda: False)
        assert not vpn.status()

    def test_interface_plus_probe(self, monkeypatch):
        monkeypatch.setattr(vpn, "_interface_up", lambda: True)
        monkeypatch.setenv("SECPT_VPN_PROBE_URL", "http://10.0.100.58")
        monkeypatch.setattr(vpn, "probe", lambda: True)
        assert vpn.status()

    def test_probe_body_ok(self, monkeypatch):
        import urllib.response

        monkeypatch.setenv("SECPT_VPN_PROBE_URL", "http://10.0.100.58")

        class _R:
            status = 200

            def read(self, n):
                return b'{"status":"ok"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _R())
        assert vpn.probe()


class TestUpDown:
    def _mk_cfg(self, tmp_path, monkeypatch, vtype="openvpn"):
        cfg = tmp_path / ("client.ovpn" if vtype == "openvpn" else "wg0.conf")
        cfg.write_text("x\n", encoding="utf-8")
        monkeypatch.setenv("SECPT_VPN_TYPE", vtype)
        monkeypatch.setenv("SECPT_VPN_CONFIG", str(cfg))
        monkeypatch.setenv("SECPT_VPN_SUDO", "0")
        monkeypatch.setattr(vpn, "PID_FILE", tmp_path / "run" / "vpn.pid")
        return cfg

    def test_up_success_openvpn(self, tmp_path, monkeypatch):
        self._mk_cfg(tmp_path, monkeypatch)
        monkeypatch.delenv("SECPT_VPN_PROBE_URL", raising=False)
        state = {"up": False}
        monkeypatch.setattr(vpn, "_interface_up", lambda: state["up"])  # 拉起前无接口
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "openvpn" in cmd:
                state["up"] = True  # 模拟 openvpn 守护进程拉起接口
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, msg = vpn.up(timeout_s=5)
        assert ok and "openvpn" in msg
        assert calls and "openvpn" in calls[0]

    def test_up_missing_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VPN_TYPE", "openvpn")
        monkeypatch.setenv("SECPT_VPN_CONFIG", str(tmp_path / "nope.ovpn"))
        monkeypatch.setattr(vpn, "_interface_up", lambda: False)  # 隔离宿主真实接口
        ok, msg = vpn.up()
        assert not ok and "不存在" in msg

    def test_up_not_configured(self, monkeypatch):
        monkeypatch.setenv("SECPT_VPN_TYPE", "none")
        ok, msg = vpn.up()
        assert not ok and "未配置" in msg

    def test_down_by_pid(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VPN_TYPE", "openvpn")
        pid = tmp_path / "run" / "vpn.pid"
        pid.parent.mkdir(parents=True)
        pid.write_text("12345", encoding="utf-8")
        monkeypatch.setattr(vpn, "PID_FILE", pid)
        killed = []

        def fake_run(cmd, **kw):
            killed.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, msg = vpn.down()
        assert ok and killed and "12345" in killed[0]
        assert not pid.exists()
