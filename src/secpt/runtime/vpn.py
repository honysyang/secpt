"""VPN 自举：系统自己拉起/检查/关闭 VPN 连接（benchmark 类内网任务前置）。

配置（.env / export）：
- SECPT_VPN_TYPE=openvpn|wireguard|none（默认 none = 不管理，假定用户已连）
- SECPT_VPN_CONFIG=<ovpn 或 wg 配置文件路径>（必填，type 非 none 时）
- SECPT_VPN_PROBE_URL=http://10.0.100.58（连通探针；可选，up 后自动验证）
- SECPT_VPN_SUDO=1（openvpn/wg-quick 需要 root：用 sudo；默认 1）

行为：
- `status()`：有接口(tun*/wg*) 且（若配探针）探针可达 → True
- `up()`：拉起守护进程（openvpn --daemon / wg-quick up），等探针通过或超时
- `down()`：关闭并清理
- solve --vpn：任务前自动 up + 验证，并把沙箱切 host 网络（容器才能走路由表中的 VPN 网段）

安全：配置文件路径从 env 读，凭证（user/pass）建议用 openvpn 的
auth-user-pass 文件（600 权限），不要把密码写进 .env。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

PID_FILE = Path(os.environ.get("SECPT_VAR", "var")) / "run" / "vpn.pid"


def _cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def vpn_type() -> str:
    return _cfg("SECPT_VPN_TYPE", "none").lower()


def vpn_configured() -> bool:
    return vpn_type() in ("openvpn", "wireguard") and bool(_cfg("SECPT_VPN_CONFIG"))


def _sudo() -> list[str]:
    return ["sudo", "-n"] if _cfg("SECPT_VPN_SUDO", "1") == "1" else []


def _interface_up() -> bool:
    try:
        out = subprocess.run(
            ["ip", "-brief", "addr"], capture_output=True, text=True, timeout=10
        ).stdout
        return any(x in out for x in ("tun", "wg"))
    except (OSError, subprocess.TimeoutExpired):
        return False


def probe() -> bool:
    """探针检查（SECPT_VPN_PROBE_URL，默认 http://10.0.100.58，只判 status==ok 或 2xx）。"""
    url = _cfg("SECPT_VPN_PROBE_URL", "http://10.0.100.58")
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=8) as resp:
            body = resp.read(4096).decode(errors="replace")
            return resp.status == 200 and (
                '"status":"ok"' in body or '"status": "ok"' in body or len(body) > 0
            )
    except Exception:
        return False


def status() -> bool:
    """VPN 就绪 = 接口存在 且（若配探针）探针可达。"""
    if not _interface_up():
        return False
    if _cfg("SECPT_VPN_PROBE_URL"):  # 配了探针才严判
        return probe()
    return True


def up(timeout_s: float = 60.0) -> tuple[bool, str]:
    """拉起 VPN 并验证。返回 (ok, msg)。"""
    if not vpn_configured():
        return False, "未配置 VPN：SECPT_VPN_TYPE=openvpn|wireguard + SECPT_VPN_CONFIG=<路径>"
    if status():
        return True, "VPN 已连通（复用现有连接）"
    cfg = Path(_cfg("SECPT_VPN_CONFIG"))
    if not cfg.is_file():
        return False, f"VPN 配置文件不存在: {cfg}"
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)

    if vpn_type() == "openvpn":
        log = PID_FILE.with_suffix(".log")
        cmd = [
            *_sudo(),
            "openvpn",
            "--config",
            str(cfg),
            "--daemon",
            "--writepid",
            str(PID_FILE),
            "--log-append",
            str(log),
        ]
    else:  # wireguard
        cmd = [*_sudo(), "wg-quick", "up", str(cfg)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, f"VPN 启动失败: {(r.stderr or r.stdout)[:300]}"

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if status():
            return True, f"VPN 已连通（{vpn_type()}，{cfg.name}）"
        time.sleep(2.0)
    return False, f"VPN 接口已拉起但探针 {timeout_s}s 内未通过——请检查配置/凭证"


def down() -> tuple[bool, str]:
    """关闭 VPN。"""
    if vpn_type() == "wireguard" and _cfg("SECPT_VPN_CONFIG"):
        r = subprocess.run(
            [*_sudo(), "wg-quick", "down", _cfg("SECPT_VPN_CONFIG")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0, (r.stderr or "wireguard 已关闭")[:200]
    if PID_FILE.is_file():
        try:
            pid = PID_FILE.read_text().strip()
            subprocess.run([*_sudo(), "kill", pid], capture_output=True, timeout=10)
        finally:
            PID_FILE.unlink(missing_ok=True)
        return True, "openvpn 已关闭"
    return True, "无托管的 VPN 进程"
