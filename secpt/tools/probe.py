"""探针 wrapper（M3-3，§2 表：tools/probe.py"wrapper 注入"）。

三件套（拍板③，§69）：
- 配置：profile.toml ``[probes]`` 段的 enable_http_probe 开关（R6：独立开关
  默认关，关=registry 不开窗零开销）；
- 后端：注入式 CaptureBackend 协议——默认 mock = ScriptedCapture（伪 HTTP
  事件源，meta 诚实标 mock:true、snapshot 落真文件）；M4-3 起 mitmproxy =
  MitmCapture 真抓包（装配期 pre-flight fail-closed，运行窗内可降级，
  见 probe_mitm.py）；
- 执行窗：wrap_execution 把"一次工具执行"包进起抓/收抓之间——动作发生时
  才有流量，取证窗口跟着执行走（denied/递归拒绝不在窗内 = 没执行没流量）。

分层红线：probe 属 tools 层，禁 import judge 层；为避免与 registry 循环
import，ToolOutput 用 TYPE_CHECKING 引入（registry 运行时 import probe，
probe 不运行时依赖 registry）。
"""

from __future__ import annotations

import tomllib
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from pydantic import BaseModel

from secpt.errors import SecptError

if TYPE_CHECKING:
    from collections.abc import Callable

    from secpt.tools.registry import ToolOutput

_log = structlog.get_logger(__name__)


class ProbeConfig(BaseModel):
    """探针装配快照：enabled 开关 + source（溯源路径，关闭档不署名防误导）。"""

    enabled: bool = False
    source: str | None = None


def load_probe_config(path: str | None) -> ProbeConfig:
    """读 profile.toml 装配 ProbeConfig（三档语义仿 judge.profile）。

    - path 为 None 或文件不存在或无 [probes] 段 → 默认关闭（R6：探针独立
      开关，缺配置零开销，不拒跑）；
    - 坏 toml / enable_http_probe 非 bool = 操作方笔误，E_PROFILE 拒跑
      （静默降级会掩盖真问题，同 judge 配置口径）。
    """
    if not path:
        return ProbeConfig()
    file = Path(path)
    if not file.is_file():
        return ProbeConfig()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise SecptError(
            "profile.toml 解析失败",
            code="E_PROFILE",
            context={"path": str(file), "reason": str(exc)},
        ) from exc

    probes = data.get("probes")
    if not isinstance(probes, dict):
        return ProbeConfig(enabled=False, source=None)
    raw = probes.get("enable_http_probe", False)
    if not isinstance(raw, bool):
        raise SecptError(
            "probes.enable_http_probe 必须是 bool",
            code="E_PROFILE",
            context={"path": str(file), "got": type(raw).__name__},
        )
    if not raw:
        return ProbeConfig(enabled=False, source=None)
    return ProbeConfig(enabled=True, source=str(file))


def note_for(cfg: ProbeConfig, backend: str = "mock") -> str:
    """装配注记一句话（loop enter() 烙 audit，同 judge.note_for 手式）。

    backend 显式入参（M4-3）：真抓包与伪事件源在账面上必须可分辨——读账的
    人要知道流量是抓来的还是脚本演的。默认 "mock" 保持 M3 老口径逐字节一致。
    """
    if not cfg.enabled:
        return "探针关闭（enable_http_probe=false，零开销）"
    src = cfg.source or "?"
    if backend == "mitmproxy":
        return f"探针开启（http 通道，profile={src}，后端 mitmproxy=MitmCapture 真抓包）"
    return f"探针开启（http 通道，profile={src}，后端 mock=ScriptedCapture）"


class CaptureEvent(BaseModel):
    """一次观测事件：channel + 事件元数据 + 快照文件指针（loop 转基元参数落账）。"""

    channel: str
    meta: dict[str, Any]
    snapshot_ref: str


class CaptureBackend(Protocol):
    """抓包后端契约：start/stop 同形对称，event 列表只在 stop 交割。

    为什么是 start/stop 而非"逐请求回调"：窗口语义由 registry 控制帧（拍板②
    执行窗全包），后端只对"窗内发生了什么"负责——M4 接 mitmproxy/pcap 时
    同形替换，不为 MVP 预设 callback 形态。
    """

    def start(self) -> None:
        """开窗。若后端起不来请抛错——执行窗捕获后降级告警（可降级器官）。"""
        ...

    def stop(self) -> list[CaptureEvent]:
        """收窗交割窗内事件（快照文件应已在 stop 内落盘，指针须真实存在）。"""
        ...


_SNAPSHOT_BODY = (
    "(mock HTTP 交互)\nGET / HTTP/1.1\nHost: target\nUser-Agent: secpt-probe\n\n"
    "HTTP/1.1 200 OK\nContent-Type: text/html\n\n<html>ok</html>\n"
)


class ScriptedCapture:
    """默认 mock 后端：伪 HTTP 事件源。

    诚实三件：meta 一律叠加 ``mock: True``（读账的人不会被伪流量骗）；snapshot
    是真写入的文件（stop 时落盘 raw_dir，指针绝不分叉向虚空）；channel
    固定 http（L1132：MVP 只盯 HTTP）。窗口可重复开合，每窗都交割同一份脚本
    事件——幂等的"背景噪声"，用于演示执行窗确实跟着动作走。
    """

    channel = "http"

    def __init__(
        self, raw_dir: Path | str = "raw", events: tuple[dict[str, Any], ...] | None = None
    ) -> None:
        self._raw_dir = Path(raw_dir)
        # 缺省脚本 = 一条匿名 GET：演示路径开箱即有一条观测，不必每处配脚本。
        self._events = (
            events
            if events is not None
            else (
                {
                    "meta": {"method": "GET", "url": "http://target/", "status": 200},
                    "body": _SNAPSHOT_BODY,
                },
            )
        )

    def start(self) -> None:
        return None

    def stop(self) -> list[CaptureEvent]:
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        out: list[CaptureEvent] = []
        for scripted in self._events:
            path = self._raw_dir / f"http_{uuid.uuid4().hex}.txt"
            path.write_text(str(scripted.get("body", "")), encoding="utf-8")
            meta = {**scripted.get("meta", {}), "mock": True}
            out.append(CaptureEvent(channel=self.channel, meta=meta, snapshot_ref=str(path)))
        return out


def build_backend(name: str, raw_dir: Path | str) -> CaptureBackend:
    """按 SECPT_PROBE_BACKEND 装配后端：mock 默认；mitmproxy=M4-3 真抓包接线。

    mitmproxy 档装配期 pre-flight（fail-closed）：mitmdump 未装/不可执行 →
    E_PROBE 拒跑，绝不静默换 mock 假装在抓；运行窗内的瞬时故障才降级
    （wrap_execution 契约）。函数级 import 破循环：probe_mitm 顶层依赖本
    模块的 CaptureEvent，probe 不在顶层反向依赖 probe_mitm。
    """
    if name == "mock":
        return ScriptedCapture(raw_dir=raw_dir)
    if name == "mitmproxy":
        from secpt.tools.probe_mitm import MitmCapture, default_mitmdump_bin, preflight_mitmdump

        mitmdump_bin = default_mitmdump_bin()
        preflight_mitmdump(mitmdump_bin)
        return MitmCapture(raw_dir=raw_dir, mitmdump_bin=mitmdump_bin)
    raise SecptError(
        "未知探针后端", code="E_PROBE", context={"backend": name, "known": ("mock", "mitmproxy")}
    )


def _safe_stop(backend: CaptureBackend) -> list[CaptureEvent]:
    """收抓统一降级口：stop 炸了只告警不外抛（FT：可降级器官绝不挡回执交割）。"""
    try:
        return backend.stop()
    except Exception as exc:  # noqa: BLE001  可降级器官兜底（设计边界，见 docstring）
        _log.warning("probe_stop_degraded", error=str(exc))
        return []


def wrap_execution(
    backend: CaptureBackend | None, execute: Callable[[], ToolOutput]
) -> tuple[ToolOutput, list[CaptureEvent]]:
    """一次工具执行的观测窗（拍板②执行窗全包）。

    - backend=None：零开销最短路——直接执行，事件恒空（关档默认行为）；
    - start 失败：告警降级，照常执行（抓不到流量不该挡住渗透动作）；
    - execute 本身异常：先收窗再原样上抛（窗口开过就要收，不悬窗）；
    - stop 失败：告警降级，交割部分/空事件，回执完好。

    Returns:
        (工具回执, 窗内 CaptureEvent 列表)。
    """
    if backend is None:
        return execute(), []
    started = False
    try:
        backend.start()
        started = True
    except Exception as exc:  # noqa: BLE001  可降级器官兜底（见上方 docstring）
        _log.warning("probe_start_degraded", error=str(exc))
    try:
        output = execute()
    except BaseException:
        if started:
            _safe_stop(backend)
        raise
    events = _safe_stop(backend) if started else []
    return output, events


__all__ = [
    "CaptureBackend",
    "CaptureEvent",
    "ProbeConfig",
    "ScriptedCapture",
    "build_backend",
    "load_probe_config",
    "note_for",
    "wrap_execution",
]
