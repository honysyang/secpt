"""oob 插件：带审计的带外（OOB）HTTP 回调监听器。

用途（通用，非跑分专属）：盲 SSRF / XXE / 盲 RCE / 反序列化的带外验证——
目标能回连时，用一次性 URL 证明"请求真的从目标发出"，比看回显可靠。

安全与治理：
- 显式装配才生效（profile/工具映射声明 oob）；
- 绑定地址/端口可配（SECPT_OOB_BIND / SECPT_OOB_HOST / SECPT_OOB_PORT）；
- 每个请求落盘 var/oob/<token>.jsonl（来源 IP、方法、路径、头、截断 body），
  事件上限 500 条，防被刷爆内存；
- 只监听、不主动外连（红线：不关 TLS 校验、不做未授权外发）。

动作：start（返回回调 URL 基址）/ check（取事件）/ stop（关停）。
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

SCHEMA = {
    "type": "object",
    "description": (
        "带外回调监听器（盲 SSRF/XXE/盲 RCE 验证）。start 返回 http://<host>:<port>/<token> "
        "作为投递 URL（任意子路径都会记录）；check 取最近回调；stop 关闭。"
        "目标需能访问该地址（VPN/内网场景用 SECPT_OOB_HOST 指定可达 IP）。"
    ),
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start", "check", "stop"],
            "description": "start/check/stop",
        },
        "bind": {"type": "string", "description": "监听地址（默认 SECPT_OOB_BIND 或 0.0.0.0）"},
        "port": {"type": "integer", "description": "监听端口（默认 0=自动分配）"},
        "ttl_s": {"type": "integer", "description": "监听有效期秒数（默认 3600）"},
        "limit": {"type": "integer", "description": "check 返回事件条数（默认 30）"},
    },
    "required": ["action"],
}

_MAX_EVENTS = 500
_MAX_BODY = 4096
_STATE: dict = {
    "server": None,
    "thread": None,
    "token": "",
    "events": [],
    "log": "",
    "started": 0.0,
    "ttl": 3600,
    "bind": "",
    "port": 0,
}
_LOCK = threading.Lock()


def _advertised_ip(bind: str) -> str:
    """给目标用的可达 IP：SECPT_OOB_HOST > tun*/wg* 地址 > bind > 默认路由地址。"""
    env = os.environ.get("SECPT_OOB_HOST", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=3
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) > 3 and (parts[1].startswith("tun") or parts[1].startswith("wg")):
                return parts[3].split("/")[0]
    except Exception:
        pass
    if bind and bind not in ("0.0.0.0", "::"):
        return bind
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _append_event(ev: dict) -> None:
    with _LOCK:
        _STATE["events"].append(ev)
        if len(_STATE["events"]) > _MAX_EVENTS:
            _STATE["events"] = _STATE["events"][-_MAX_EVENTS:]
        log = _STATE["log"]
    if log:
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # 静音：访问记录走 JSONL
        pass

    def _record(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, _MAX_BODY)) if n > 0 else b""
        except Exception:
            body = b""
        headers: dict[str, str] = {}
        for k, v in list(self.headers.items())[:20]:
            headers[k[:60]] = str(v)[:300]
        ev = {
            "ts": time.time(),
            "remote": self.client_address[0] if self.client_address else "",
            "method": self.command,
            "path": self.path[:1000],
            "headers": headers,
            "body": body.decode("utf-8", errors="replace"),
        }
        _append_event(ev)
        payload = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _record
    do_POST = _record
    do_PUT = _record
    do_PATCH = _record
    do_DELETE = _record
    do_HEAD = _record
    do_OPTIONS = _record


def _stop_locked() -> None:
    srv = _STATE.get("server")
    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    _STATE.update({"server": None, "thread": None, "token": "", "events": [], "log": ""})


def _expired() -> bool:
    started = float(_STATE.get("started") or 0)
    ttl = int(_STATE.get("ttl") or 0)
    return bool(started and ttl > 0 and time.time() - started > ttl)


def oob(args: dict) -> str:
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    if action == "start":
        with _LOCK:
            if _STATE.get("server") is not None and not _expired():
                base = f"http://{_STATE['bind']}:{_STATE['port']}/{_STATE['token']}"
                return json.dumps(
                    {"ok": True, "already_running": True, "url_base": base, "log": _STATE["log"]},
                    ensure_ascii=False,
                )
            _stop_locked()
            bind = str(args.get("bind") or os.environ.get("SECPT_OOB_BIND", "0.0.0.0"))
            try:
                port = int(args.get("port") or os.environ.get("SECPT_OOB_PORT", "0") or 0)
                ttl = int(args.get("ttl_s") or 3600)
            except (TypeError, ValueError):
                return "Error: port/ttl_s 必须是整数"
            try:
                srv = http.server.ThreadingHTTPServer((bind, port), _Handler)
            except OSError as e:
                return f"Error: 监听 {bind}:{port} 失败: {e}"
            srv.daemon_threads = True
            token = uuid.uuid4().hex[:12]
            var = Path(os.environ.get("SECPT_VAR", "var")) / "oob"
            var.mkdir(parents=True, exist_ok=True)
            log = str((var / f"{token}.jsonl").resolve())
            _STATE.update(
                {
                    "server": srv,
                    "token": token,
                    "events": [],
                    "log": log,
                    "started": time.time(),
                    "ttl": ttl,
                    "bind": bind,
                    "port": srv.server_address[1],
                }
            )
            th = threading.Thread(target=srv.serve_forever, daemon=True, name=f"oob-{token}")
            _STATE["thread"] = th
            th.start()
            host = _advertised_ip(bind)
            return json.dumps(
                {
                    "ok": True,
                    "url_base": f"http://{host}:{srv.server_address[1]}/{token}",
                    "bind": f"{bind}:{srv.server_address[1]}",
                    "log": log,
                    "ttl_s": ttl,
                    "note": (
                        "把 url_base 或 url_base/<任意子路径> 作为投递地址；"
                        "目标需能访问该 IP，若不通用 SECPT_OOB_HOST 指定可达地址。"
                    ),
                },
                ensure_ascii=False,
            )
    if action == "check":
        with _LOCK:
            if _expired():
                _stop_locked()
                return json.dumps({"ok": True, "expired": True, "events": []}, ensure_ascii=False)
            events = list(_STATE.get("events") or [])
        try:
            limit = max(1, min(200, int(args.get("limit") or 30)))
        except (TypeError, ValueError):
            limit = 30
        return json.dumps(
            {
                "ok": True,
                "running": _STATE.get("server") is not None,
                "total": len(events),
                "events": events[-limit:],
            },
            ensure_ascii=False,
        )
    if action == "stop":
        with _LOCK:
            total = len(_STATE.get("events") or [])
            log = _STATE.get("log") or ""
            _stop_locked()
        return json.dumps(
            {"ok": True, "stopped": True, "total": total, "log": log}, ensure_ascii=False
        )
    return "Error: action 必须是 start/check/stop"


def register() -> dict:
    return {"oob": (SCHEMA, oob)}
