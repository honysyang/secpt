"""mitmproxy 抓包后端（M4-3，§69 图纸位兑现）：CaptureBackend 同形替换 ScriptedCapture。

为什么是"mitmdump 子进程 + addon 落 JSONL"而不是库内嵌 mitmproxy API：
- 进程边界即故障边界——mitmproxy 的证书生成/事件循环/信号处理完全不进
  主进程，起不来最多窗内降级，绝不拖垮 loop；
- 一发一会话同手式（McpStdioClient/NmapTool）：start 起进程、stop 收尸，
  无跨窗生命周期泄漏。

fail-closed 两层语义（与 M3 契约分工，各管各的窗口）：
- 装配期（build_backend → preflight_mitmdump）：操作方点名 mitmproxy 档，
  mitmdump 未装/不可执行 → E_PROBE 拒跑，绝不静默换 mock 假装在抓；
- 运行窗内（start 失败/端口不就绪）：维持 M3 可降级器官契约——
  wrap_execution 告警降级照常执行，抓不到流量不该挡住渗透动作。

诚实边界：CaptureEvent.meta 只带 method/url/status 真实字段，``mock`` 键
永不出现（真流量如实标注）；snapshot 是真文件（JSONL 记录物化落盘 raw_dir）。
R0 obs 锚与判票口径零改动：meta 形状与 ScriptedCapture 缺省脚本同构（仅少
mock 键），loop 侧 observations.append 原样消费，不需要任何适配层。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import structlog

from secpt.errors import SecptError
from secpt.tools.probe import CaptureEvent

_log = structlog.get_logger(__name__)

MITMDUMP_BIN_ENV = "SECPT_MITMDUMP_BIN"
_FLOW_OUT_ENV = "SECPT_MITMDUMP_FLOW_OUT"

# addon 脚本（包内静态文件，由 mitmdump 子进程加载，secpt 主进程不 import）。
_ADDON_PATH = Path(__file__).with_name("_mitm_addon.py")

_READY_POLL_S = 0.1  # 就绪轮询步长
_SETTLE_S = 0.2  # TCP 就绪后的沉降等待（mitmdump 开听后 addon 才开始记账）
_PREFLIGHT_TIMEOUT_S = 15.0
_DEFAULT_READY_TIMEOUT_S = 15.0
_TERMINATE_WAIT_S = 5.0


def default_mitmdump_bin() -> str:
    """mitmdump 二进制定位：SECPT_MITMDUMP_BIN 优先，缺省 PATH 找 mitmdump。"""
    return os.environ.get(MITMDUMP_BIN_ENV, "mitmdump").strip() or "mitmdump"


def preflight_mitmdump(bin_path: str) -> None:
    """装配期 pre-flight（fail-closed）：``--version`` 探活，未装/不可执行 → E_PROBE。

    为什么 pre-flight 在装配期而不是运行窗内：任务书口径"mitmproxy 未装/
    起不来 → E_PROBE 拒跑"——操作方点名要真抓包，装配期就该发现少零件，
    绝不带病进场换 mock 演戏；运行窗内的瞬时故障才走 M3 可降级器官契约。
    """
    try:
        proc = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecptError(
            "mitmproxy 后端 pre-flight 失败：mitmdump 未安装或不可执行",
            code="E_PROBE",
            context={"backend": "mitmproxy", "binary": bin_path, "reason": str(exc)},
        ) from exc
    if proc.returncode != 0:
        raise SecptError(
            "mitmproxy 后端 pre-flight 失败：mitmdump --version 非零退出",
            code="E_PROBE",
            context={"backend": "mitmproxy", "binary": bin_path, "returncode": proc.returncode},
        )


def _free_port() -> int:
    """临时端口：bind 0 由内核分配（演示/测试不与常驻服务抢端口）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MitmCapture:
    """真抓包后端：start 起 mitmdump 监听 TCP PROXY 端口，stop 交割 CaptureEvent。

    与 ScriptedCapture 同形（start/stop 对称、事件列表只在 stop 交割、
    snapshot 真文件），差异只在事件来源：脚本是演的，这里是抓的。
    ``proxy_port`` 在 start 后暴露，供测试/演示把流量指进代理。
    """

    channel = "http"

    def __init__(
        self,
        raw_dir: Path | str = "raw",
        *,
        mitmdump_bin: str = "mitmdump",
        ready_timeout_s: float = _DEFAULT_READY_TIMEOUT_S,
    ) -> None:
        self._raw_dir = Path(raw_dir)
        self._bin = mitmdump_bin
        self._ready_timeout_s = ready_timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._proxy_port = 0
        self._flow_path: Path | None = None
        self._confdir: str | None = None

    @property
    def proxy_port(self) -> int:
        """start 后暴露的代理端口（start 前为 0，测试/演示据此路由流量）。"""
        return self._proxy_port

    def start(self) -> None:
        """开窗：选临时端口 → 起 mitmdump → 就绪轮询（TCP connect 探活）。

        confdir 走临时目录：证书等 mitmproxy 自留地不落用户 HOME，窗毕即清。
        """
        if self._proc is not None:
            raise SecptError(
                "MitmCapture 已在窗内（start/stop 必须成对）",
                code="E_PROBE",
                context={"proxy_port": self._proxy_port},
            )
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._proxy_port = _free_port()
        self._flow_path = self._raw_dir / f"mitm_flows_{uuid.uuid4().hex}.jsonl"
        self._confdir = tempfile.mkdtemp(prefix="secpt-mitm-")
        argv = [
            self._bin,
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            str(self._proxy_port),
            "--set",
            f"confdir={self._confdir}",
            "-s",
            str(_ADDON_PATH),
            "-q",
        ]
        env = {**os.environ, _FLOW_OUT_ENV: str(self._flow_path)}
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
            )
        except OSError as exc:
            self._terminate()
            raise SecptError(
                "mitmdump 进程启动失败",
                code="E_PROBE",
                context={"binary": self._bin, "reason": str(exc)},
            ) from exc
        if not self._wait_ready():
            self._terminate()
            raise TimeoutError(
                f"mitmdump 代理端口 {self._proxy_port} 在 {self._ready_timeout_s}s 内未就绪"
            )

    def stop(self) -> list[CaptureEvent]:
        """收窗：收尸 → 逐行解析 JSONL → 每条记录物化快照并交割事件。

        单行解析失败只告警跳过（部分行不拖垮整窗交割，与 _safe_stop 降级
        语义同向）；未开窗（stop 无 start）= 空交割，幂等不炸。
        """
        self._terminate()
        if self._flow_path is None or not self._flow_path.is_file():
            return []
        events: list[CaptureEvent] = []
        for line in self._flow_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                _log.warning("mitm_flow_record_skipped", path=str(self._flow_path))
                continue
            if isinstance(record, dict):
                events.append(self._materialize(record))
        return events

    def _wait_ready(self) -> bool:
        """就绪轮询：TCP connect 探活 + 进程提前退出即断（不傻等到超时）。"""
        deadline = time.monotonic() + self._ready_timeout_s
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                return False
            try:
                with socket.create_connection(
                    ("127.0.0.1", self._proxy_port), timeout=_READY_POLL_S
                ):
                    time.sleep(_SETTLE_S)
                    return True
            except OSError:
                time.sleep(_READY_POLL_S)
        return False

    def _terminate(self) -> None:
        """收尸：terminate → 宽限 → kill；confdir 临时目录即清（幂等可重入）。"""
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_TERMINATE_WAIT_S)
        if self._confdir:
            shutil.rmtree(self._confdir, ignore_errors=True)
            self._confdir = None

    def _materialize(self, record: dict[str, Any]) -> CaptureEvent:
        """JSONL 记录物化：快照落真文件（诚实锚点），meta 只带真实字段。"""
        snapshot = self._raw_dir / f"http_{uuid.uuid4().hex}.txt"
        text = (
            f">>> {record.get('method', '?')} {record.get('url', '?')}\n"
            f"{record.get('request', '')}\n"
            f"<<< HTTP {record.get('status', '?')}\n"
            f"{record.get('response', '')}\n"
        )
        snapshot.write_text(text, encoding="utf-8")
        try:
            status = int(record.get("status", 0))
        except (TypeError, ValueError):
            status = 0
        meta = {
            "method": str(record.get("method", "?")),
            "url": str(record.get("url", "?")),
            "status": status,
        }
        return CaptureEvent(channel=self.channel, meta=meta, snapshot_ref=str(snapshot))


__all__ = ["MITMDUMP_BIN_ENV", "MitmCapture", "default_mitmdump_bin", "preflight_mitmdump"]
