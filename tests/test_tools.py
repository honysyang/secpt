"""S5：工具层直接可测、白名单、路径逃逸、probe 双路径（S5 追加验收四条）。

四条验收：
- 每个工具不经 gateway 直接调用一次并返回 ProcResult；
- shell 白名单：非白名单前缀 exit_code=126 且不执行；
- fs 路径逃逸被拦（../../etc/passwd 与绝对路径都返回 blocked，读不到文件）；
- probe 用本地监听端口验证 open/closed，不依赖外网。
"""

from __future__ import annotations

import http.server
import socket
import threading

from pa.infra.executor import ProcResult
from pa.tools import REGISTRY, manifest
from pa.tools.base import ToolContext


def _ctx(tmp_mission) -> ToolContext:
    return ToolContext(workspace=tmp_mission, timeout=10)


def test_registry_and_direct_calls(tmp_mission):
    """追加验收一：六个工具都注册、都能不经 gateway 直接调用。"""
    assert set(REGISTRY) == {"dns", "shell", "http", "fs", "probe", "tls"}
    assert len(manifest()) == 6 and all("fn" not in m for m in manifest())
    ctx = _ctx(tmp_mission)

    # fs：直接读 workspace 里的文件
    (tmp_mission / "note.txt").write_text("hello", encoding="utf-8")
    r = REGISTRY["fs"].fn({"op": "read", "path": "note.txt"}, ctx)
    assert isinstance(r, ProcResult) and r.exit_code == 0 and r.stdout == b"hello"
    r = REGISTRY["fs"].fn({"op": "list", "path": "."}, ctx)
    assert r.exit_code == 0 and b"note.txt" in r.stdout

    # dns：localhost 必然可解析（不依赖外网）
    r = REGISTRY["dns"].fn({"target": "localhost"}, ctx)
    assert isinstance(r, ProcResult) and r.exit_code == 0 and b"ip=" in r.stdout
    assert r.meta and r.meta.get("ips")

    # shell：白名单内命令直接执行
    r = REGISTRY["shell"].fn({"cmd": "ls note.txt"}, ctx)
    assert isinstance(r, ProcResult) and r.exit_code == 0 and b"note.txt" in r.stdout

    # http：本地 http.server，不碰外网
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        lambda *a, **k: http.server.SimpleHTTPRequestHandler(
            *a, directory=str(tmp_mission), **k
        ),
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/note.txt"
        r = REGISTRY["http"].fn({"method": "GET", "url": url}, ctx)
        assert isinstance(r, ProcResult) and r.exit_code == 200 and b"hello" in r.stdout
    finally:
        srv.shutdown()

    # tls：对非 TLS 端口握手失败 → exit 1（ happy path 依赖证书，见
    # test_tls_clue_extraction 的构造证据路径）
    r = REGISTRY["tls"].fn({"target": "127.0.0.1", "port": srv.server_address[1]}, ctx)
    assert isinstance(r, ProcResult) and r.exit_code == 1 and r.stderr

    # probe：open/closed 两条路径见 test_probe_open_closed


def test_probe_open_closed(tmp_mission):
    """追加验收四：本地监听端口验证 open/closed。"""
    ctx = _ctx(tmp_mission)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    open_port = srv.getsockname()[1]
    try:
        r = REGISTRY["probe"].fn({"target": "127.0.0.1", "port": open_port}, ctx)
        assert isinstance(r, ProcResult) and r.exit_code == 0 and r.stdout == b"open\n"
    finally:
        srv.close()
    closed_port = open_port + 1  # 刚释放的端口邻居，几乎必然 closed
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", closed_port))  # 占用成功说明本可连接 → 换一个
        s.close()
        closed_port += 1
    except OSError:
        pass
    r = REGISTRY["probe"].fn({"target": "127.0.0.1", "port": closed_port}, ctx)
    assert isinstance(r, ProcResult) and r.exit_code == 1 and r.stdout == b"closed\n"


def test_shell_whitelist_blocks_and_does_not_execute(tmp_mission):
    """追加验收二：非白名单前缀 exit_code=126，进程没有执行。"""
    ctx = _ctx(tmp_mission)
    marker = tmp_mission / "sentinel"
    r = REGISTRY["shell"].fn({"cmd": f"touch {marker}"}, ctx)
    assert r.exit_code == 126 and b"blocked:command_not_allowed" in r.stderr
    assert not marker.exists()  # 没执行
    r = REGISTRY["shell"].fn({"cmd": "rm -rf /"}, ctx)
    assert r.exit_code == 126
    r = REGISTRY["shell"].fn({"cmd": ""}, ctx)
    assert r.exit_code == 126


def test_fs_path_escape_blocked(tmp_mission):
    """追加验收三：路径逃逸返回 blocked，读不到文件。"""
    ctx = _ctx(tmp_mission)
    for bad in ("../../etc/passwd", "/etc/passwd", "../workspace/../../etc/passwd"):
        r = REGISTRY["fs"].fn({"op": "read", "path": bad}, ctx)
        assert isinstance(r, ProcResult) and r.exit_code == 126, bad
        assert r.stdout == b"" and b"blocked:path_escape" in r.stderr, bad
    # workspace 内部正常，深层相对路径也正常
    sub = tmp_mission / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("in", encoding="utf-8")
    r = REGISTRY["fs"].fn({"op": "read", "path": "a/b/f.txt"}, ctx)
    assert r.exit_code == 0 and r.stdout == b"in"


def test_probe_timeout_clamp(monkeypatch, tmp_mission):
    """质量项②：probe 单连接超时 = min(ctx.timeout, 往返天花板)，
    调用方超预算时被钳住；env 缺失/非法时退默认 5.0。"""
    from pa import envs
    from pa.tools import probe as probe_mod

    seen: list[float] = []

    class _FakeSock:
        def __init__(self, *a, **k) -> None:
            pass

        def settimeout(self, t: float) -> None:
            seen.append(t)

        def connect_ex(self, addr) -> int:
            return 1  # closed

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe_mod.socket, "socket", _FakeSock)

    def call(ctx):
        return probe_mod.invoke({"target": "10.0.0.5", "port": 80}, ctx)

    monkeypatch.setenv("SECPT_PROBE_TIMEOUT", "5")
    call(ToolContext(workspace=tmp_mission, timeout=120))  # 泛预算被钳到 5
    call(ToolContext(workspace=tmp_mission, timeout=2))  # 调用方更小，原样保留
    assert seen == [5.0, 2.0]

    for bad in ("abc", "0", "-1"):  # 非法值：显式回退默认，而不是崩
        monkeypatch.setenv("SECPT_PROBE_TIMEOUT", bad)
        assert envs.probe_timeout() == 5.0
    monkeypatch.delenv("SECPT_PROBE_TIMEOUT")
    call(ToolContext(workspace=tmp_mission, timeout=120))
    assert seen[-1] == 5.0  # 未配置 → 默认 5.0，不再是 120
