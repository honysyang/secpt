"""TrafficRecorder：mitmproxy 编程式启动的透明流量录制（内部授权场景）。

明文录制 MITM：engine 的 bash 工具检测到本模块注入的 SECPT_PROXY_URL /
SECPT_PROXY_CA 后，会自动给子进程注入 HTTP(S)_PROXY 与 CA 环境变量，
子进程流量经本代理落 SQLite（见 store.py）。

纪律：start() 绝不抛出——import 失败 / 启动超时 / 端口占用等任何异常都只
告警并返回 None，录制层是观测设施，绝不允许打断主链路。
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from secpt.traffic.store import TrafficStore, default_traffic_dir

if TYPE_CHECKING:  # 仅类型检查期引入，运行时导入失败不影响本模块
    from mitmproxy import http as mitm_http
    from mitmproxy.tools.dump import DumpMaster

START_TIMEOUT = 15.0  # 等待代理监听地址出现的秒数
CA_WAIT_TIMEOUT = 10.0  # 等待 CA 证书生成的秒数
CA_PATH = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"

_PROXY_ENV_KEYS = ("SECPT_PROXY_URL", "SECPT_PROXY_CA")


class ProxyHandle:
    """一个运行中的录制代理句柄：对外暴露 url / ca_path / job_id。"""

    def __init__(
        self,
        url: str,
        ca_path: Path,
        job_id: str,
        master: DumpMaster | None = None,
        thread: threading.Thread | None = None,
        store: TrafficStore | None = None,
    ) -> None:
        self.url = url
        self.ca_path = ca_path
        self.job_id = job_id
        self._master = master
        self._thread = thread
        self._store = store
        self._stopped = False

    def stop(self) -> None:
        """停止代理并关库；可重复调用，任何错误吞掉。"""
        if self._stopped:
            return
        self._stopped = True
        try:
            if self._master is not None:
                self._master.shutdown()
        except Exception:
            pass
        try:
            if self._thread is not None:
                self._thread.join(timeout=5)
        except Exception:
            pass
        try:
            if self._store is not None:
                self._store.close()
        except Exception:
            pass

    def set_env(self) -> None:
        """把代理配置写进 os.environ，engine 子进程将继承。"""
        os.environ["SECPT_PROXY_URL"] = self.url
        os.environ["SECPT_PROXY_CA"] = str(self.ca_path)

    def clear_env(self) -> None:
        """清理本句柄注入的代理环境变量。"""
        for key in _PROXY_ENV_KEYS:
            os.environ.pop(key, None)


class _RecordAddon:
    """mitmproxy addon：每个完整 HTTP 交换（response 到达）落一条库。"""

    def __init__(self, store: TrafficStore) -> None:
        self._store = store

    def response(self, flow: mitm_http.HTTPFlow) -> None:
        try:
            resp = flow.response
            if resp is None:
                return
            req = flow.request
            self._store.record(
                {
                    "method": req.method,
                    "url": req.url,
                    "status": resp.status_code,
                    "req_headers": dict(req.headers.items()),
                    "req_body": req.raw_content,
                    "resp_headers": dict(resp.headers.items()),
                    "resp_body": resp.raw_content,
                    "ts": time.time(),
                }
            )
        except Exception:
            pass  # 录制失败不打断代理


class TrafficRecorder:
    """按 job 启动/停止 MITM 录制代理。"""

    def start(self, job_id: str, port: int = 0) -> ProxyHandle | None:
        """在 127.0.0.1 起 DumpMaster（随机端口），就绪后返回 ProxyHandle。

        任何失败（mitmproxy 缺失 / 超时 / 端口占用）都 print 告警并返回 None。
        """
        db_path = default_traffic_dir() / f"{job_id}.sqlite"
        try:
            store = TrafficStore(db_path)
        except Exception as exc:
            print(f"[traffic] 无法打开流量库 {db_path}: {exc}")
            return None

        box: dict[str, Any] = {}
        ready = threading.Event()

        def worker() -> None:
            try:
                import asyncio

                from mitmproxy.options import Options
                from mitmproxy.tools.dump import DumpMaster

                async def runner() -> None:
                    opts = Options(listen_host="127.0.0.1", listen_port=port)
                    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
                    master.addons.add(_RecordAddon(store))
                    box["master"] = master
                    ready.set()
                    await master.run()

                asyncio.run(runner())
            except BaseException as exc:  # 含 mitmproxy 端口占用时的 SystemExit
                box["error"] = exc
                ready.set()

        thread = threading.Thread(target=worker, daemon=True, name=f"mitm-{job_id}")
        thread.start()
        if not ready.wait(timeout=START_TIMEOUT):
            print(f"[traffic] 启动超时（{START_TIMEOUT}s），录制代理不可用: {job_id}")
            return None
        master = box.get("master")
        if master is None:
            print(f"[traffic] 启动失败: {box.get('error')!r}")
            store.close()
            return None

        # 等监听地址出现（端口 0 时需要）
        address: tuple[str, int] | None = None
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            try:
                proxyserver = master.addons.get("proxyserver")
                addrs = proxyserver.listen_addrs() if proxyserver else []
            except Exception:
                addrs = []
            if addrs:
                address = (str(addrs[0][0]), int(addrs[0][1]))
                break
            time.sleep(0.1)
        if address is None:
            print(f"[traffic] 未等到监听地址，录制代理不可用: {job_id}")
            self._abort(master, thread, store)
            return None

        # 等 CA 证书生成（engine 子进程需要它校验 HTTPS）
        ca_deadline = time.time() + CA_WAIT_TIMEOUT
        while not CA_PATH.exists() and time.time() < ca_deadline:
            time.sleep(0.1)
        if not CA_PATH.exists():
            print(f"[traffic] 等待 CA 证书超时（{CA_WAIT_TIMEOUT}s）: {CA_PATH}")
            self._abort(master, thread, store)
            return None

        return ProxyHandle(
            url=f"http://{address[0]}:{address[1]}",
            ca_path=CA_PATH,
            job_id=job_id,
            master=master,
            thread=thread,
            store=store,
        )

    @staticmethod
    def _abort(master: DumpMaster, thread: threading.Thread, store: TrafficStore) -> None:
        with contextlib.suppress(Exception):
            master.shutdown()
        thread.join(timeout=5)
        store.close()
