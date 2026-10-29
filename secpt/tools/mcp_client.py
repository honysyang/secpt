"""MCP stdio 客户端与 adapter（M4-2，蓝本 §3.5 kind=mcp 语义兑现）。

设计口径：
- **一发一会话**：每次 list_tools / tools/call 都独立起进程 → initialize 握手 →
  请求 → 收尸。与 NmapTool"每次 run 一个子进程"同手式——无长连状态、天然幂等、
  无生命周期泄漏（registry 不知道"关停"这回事，会话在 with 里自灭）。
- **双通道但同样禁 shell**：进程用 argv 列表起（Popen，无 shell=True），协议走
  stdin/stdout 的按行 JSON-RPC 2.0——MCP stdio 规约的最小子集
  （initialize / notifications/initialized / tools/list / tools/call）。
- **错误分层**：协议层失败（起不来/超时/提前断线/error 响应/形状不合法）一律
  抛 ``McpError``；注册期由 :func:`build_mcp_adapters` 捕住转告警（掉线不注册，
  装配继续），调用期由 :class:`McpTool` 捕住转 E_TOOL_FAIL 回执（与 nmap 失败
  同手式，不炸主循环）。
- **读超时用 select**：POSIX 管道 fd 可 select（环境备忘钉死 Linux），deadline
  到点杀进程上抛，绝不悬死。
- **Any 豁免边界**：``inputSchema`` 等响应体字段是 MCP 响应边界（M4 任务书口径），
  解析后立即收敛进强类型 manifest，不外溢。
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import time
import tomllib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from secpt.errors import McpError, SecptError
from secpt.tools.registry import ToolAdapter, ToolManifest, ToolOutput, manifest_hash

_log = structlog.get_logger(__name__)

_INITIALIZED_NOTIFICATION = "notifications/initialized"

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


class McpServerConfig(BaseModel):
    """[mcp.servers] 单条装配快照：name + argv 列表（禁 shell）+ 超时。"""

    name: str
    command: list[str]
    timeout_s: float = 10.0


class McpToolDef(BaseModel):
    """tools/list 的单个工具定义（MCP 响应边界收敛后的强类型卡）。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)  # Any：MCP 响应边界豁免


class McpCallResult(BaseModel):
    """tools/call 的收敛结果：文本内容 + isError 如实署名。"""

    content_text: str
    is_error: bool = False


def load_mcp_config(path: str | None) -> list[McpServerConfig]:
    """读 profile.toml 的 [[mcp.servers]] 装配配置（三档语义仿 probe/guard 手式）。

    - path 为 None / 文件不存在 / 无 [mcp] 段 → 空列表（未配置 = 零行为变化）；
    - 坏 toml / servers 非列表 / 单条缺 name 或 command / command 非字符串列表
      → E_PROFILE 拒跑（笔误显式有痕，不静默跳过）。
    """
    if not path:
        return []
    file = Path(path)
    if not file.is_file():
        return []
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise SecptError(
            "profile.toml 解析失败",
            code="E_PROFILE",
            context={"path": str(file), "reason": str(exc)},
        ) from exc
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return []
    raw_servers = mcp.get("servers", [])
    if not isinstance(raw_servers, list):
        raise SecptError(
            "mcp.servers 必须是列表（[[mcp.servers]]）",
            code="E_PROFILE",
            context={"path": str(file), "got": type(raw_servers).__name__},
        )
    return [_parse_server(raw, path=str(file)) for raw in raw_servers]


def _parse_server(raw: object, *, path: str) -> McpServerConfig:
    """单条 server 配置校验：name 必非空字符串、command 必须是非空字符串列表。"""
    if not isinstance(raw, dict):
        raise SecptError(
            "mcp.servers 条目必须是表",
            code="E_PROFILE",
            context={"path": path, "got": type(raw).__name__},
        )
    name = raw.get("name")
    command = raw.get("command")
    problems: dict[str, str] = {}
    if not isinstance(name, str) or not name.strip():
        problems["name"] = f"必填非空字符串，实得 {name!r}"
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        problems["command"] = f"必须是非空字符串列表（argv，禁 shell），实得 {command!r}"
    if problems:
        raise SecptError(
            "mcp.servers 条目配置非法", code="E_PROFILE", context={"path": path, **problems}
        )
    timeout_raw = raw.get("timeout_s", 10.0)
    try:
        timeout_s = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise SecptError(
            "mcp.servers.timeout_s 必须是数字",
            code="E_PROFILE",
            context={"path": path, "got": repr(timeout_raw)},
        ) from exc
    return McpServerConfig(name=str(name), command=list(command), timeout_s=timeout_s)


class McpStdioClient:
    """MCP stdio JSON-RPC 客户端：一发一会话，超时杀进程绝不悬死。

    ``stderr`` 直通 DEVNULL——协议走 stdout，混流会污染 JSON-RPC 帧；
    server 的自述日志不进账本（MVP 口径，需要时再接管道排空）。
    """

    def __init__(self, name: str, command: Sequence[str], *, timeout_s: float = 10.0) -> None:
        self._name = name
        self._command = list(command)
        self._timeout_s = timeout_s
        self._next_id = 0

    @property
    def server_name(self) -> str:
        """server 名（adapter 署回执/raw 文件名用，避免摸私有字段）。"""
        return self._name

    def list_tools(self) -> list[McpToolDef]:
        """起一次会话做 initialize + tools/list，收敛成 McpToolDef 列表。"""
        with self._session() as proc:
            payload = self._request_once("tools/list", proc=proc)
        raw_tools = payload.get("tools", [])
        if not isinstance(raw_tools, list):
            raise McpError(
                "tools/list 响应 tools 非列表",
                context={"server": self._name, "got": type(raw_tools).__name__},
            )
        tools: list[McpToolDef] = []
        for raw in raw_tools:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                raise McpError(
                    "tools/list 条目缺 name",
                    context={"server": self._name, "entry": repr(raw)[:120]},
                )
            schema = raw.get("inputSchema", {})
            tools.append(
                McpToolDef(
                    name=raw["name"],
                    description=str(raw.get("description", "")),
                    input_schema=schema if isinstance(schema, dict) else {},
                )
            )
        return tools

    def call(self, tool_name: str, arguments: Mapping[str, object]) -> McpCallResult:
        """起一次会话做 initialize + tools/call，文本内容拼接返回。"""
        with self._session() as proc:
            payload = self._request_once(
                "tools/call", proc=proc, params={"name": tool_name, "arguments": dict(arguments)}
            )
        content = payload.get("content", [])
        texts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return McpCallResult(
            content_text="\n".join(text for text in texts if text),
            is_error=bool(payload.get("isError", False)),
        )

    # ---- 会话与协议内躯 ------------------------------------------------------------

    @contextmanager
    def _session(self) -> Iterator[subprocess.Popen[bytes]]:
        """会话生命周期：起进程 → initialize 握手 → 交给调用方 → 收尸。"""
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv 列表无 shell；控制字符闸在 registry
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise McpError(
                f"MCP server {self._name} 启动失败",
                context={"command": self._command, "reason": str(exc)},
            ) from exc
        try:
            self._initialize(proc)
            yield proc
        finally:
            self._reap(proc)

    def _initialize(self, proc: subprocess.Popen[bytes]) -> None:
        """握手：initialize 必须有响应；initialized 是 notification 只吞不回。"""
        self._request_once("initialize", proc=proc, params={"capabilities": {}})
        assert proc.stdin is not None
        note = json.dumps({"jsonrpc": "2.0", "method": _INITIALIZED_NOTIFICATION, "params": {}})
        proc.stdin.write(note.encode("utf-8") + b"\n")
        proc.stdin.flush()

    def _request_once(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        proc: subprocess.Popen[bytes] | None = None,
    ) -> dict[str, Any]:
        """单发单收。``proc`` 缺省时自起一会话（测试直打协议层用）。"""
        if proc is None:
            with self._session() as owned:
                return self._request(method, params, owned)
        return self._request(method, params, proc)

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        proc: subprocess.Popen[bytes],
    ) -> dict[str, Any]:
        """请求/响应配对：id 自增，error 响应 / 形状不合法一律 McpError。"""
        self._next_id += 1
        rid = self._next_id
        request = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(
                f"MCP server {self._name} 请求写入失败（进程可能已退出）",
                context={"method": method, "reason": str(exc)},
            ) from exc
        line = self._readline(proc)
        try:
            resp: Any = json.loads(line)  # Any：MCP 响应边界豁免
        except json.JSONDecodeError as exc:
            raise McpError(
                f"MCP server {self._name} 响应不是合法 JSON",
                context={"method": method, "line": line[:120]},
            ) from exc
        if not isinstance(resp, dict) or resp.get("id") != rid:
            raise McpError(
                f"MCP server {self._name} 响应 id 不配对",
                context={"method": method, "expect": rid, "got": repr(resp)[:120]},
            )
        if "error" in resp:
            raise McpError(
                f"MCP server {self._name} 返回 error",
                context={"method": method, "error": repr(resp["error"])[:200]},
            )
        result = resp.get("result")
        if not isinstance(result, dict):
            raise McpError(
                f"MCP server {self._name} 响应缺 result",
                context={"method": method, "got": repr(result)[:120]},
            )
        return result

    def _readline(self, proc: subprocess.Popen[bytes]) -> str:
        """按行读 stdout：select + deadline，到点杀进程上抛，绝不悬死。"""
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + self._timeout_s
        buf = b""
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(
                    f"MCP server {self._name} 响应超时（timeout_s={self._timeout_s}s）",
                    context={"server": self._name},
                )
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise McpError(
                    f"MCP server {self._name} 提前退出（stdout 关闭）",
                    context={"server": self._name, "returncode": proc.poll()},
                )
            buf += chunk
        return buf.partition(b"\n")[0].decode("utf-8", errors="replace")

    def _reap(self, proc: subprocess.Popen[bytes]) -> None:
        """收尸三步：stdin 关闭给体面退出 → terminate → 顽固再 kill。"""
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:  # pragma: no cover — kill 后仍不退属绝症
                pass


_TYPE_MAP = {"string": "str", "integer": "int", "number": "float"}


class McpTool:
    """MCP 工具 adapter：manifest 从 inputSchema 收敛，run 转发 JSON-RPC。

    read_only 恒 False（未知即写）：MCP 工具的副作用无从断言，R1 重放门
    默认不放行——诚实优先，个别确证只读的 server 待白名单化后再声明。
    """

    manifest: ToolManifest

    def __init__(self, client: McpStdioClient, tool: McpToolDef, raw_dir: Path | str) -> None:
        params: dict[str, str] = {}
        for key, prop in tool.input_schema.get("properties", {}).items():
            kind = prop.get("type", "string") if isinstance(prop, dict) else "string"
            params[str(key)] = _TYPE_MAP.get(str(kind), "str")
        required = tuple(str(k) for k in tool.input_schema.get("required", []))
        self.manifest = ToolManifest(
            name=tool.name,
            kind="mcp",
            description=tool.description or f"MCP 工具（来自 server {client.server_name}）",
            params=params,
            required=required,
            read_only=False,
        )
        self.manifest.hash = manifest_hash(self.manifest)  # 登记卡自洽：hash 不等 registry 兜底
        self._client = client
        self._tool = tool
        self._raw_dir = Path(raw_dir)

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        """一次 MCP 调用 → 回执：协议失败转 E_TOOL_FAIL（不抛不重试，nmap 同手式）。"""
        typed = self._coerce(args)
        if isinstance(typed, str):
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: 参数转换失败 {typed}",
                raw_ref="",
            )
        try:
            result = self._client.call(self._tool.name, typed)
        except McpError as exc:
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: mcp/{self._client.server_name}/{self._tool.name} {exc}",
                raw_ref="",
            )
        raw_path = self._write_raw(result)
        exit_code = 1 if result.is_error else 0
        first_line = result.content_text.splitlines()[0][:160] if result.content_text else ""
        summary = first_line or f"exit={exit_code}"
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=exit_code,
            summary=summary,
            raw_ref=str(raw_path),
        )

    def _coerce(self, args: Mapping[str, str]) -> dict[str, object] | str:
        """str → manifest 声明类型：int/float 如实转换，失败回错误文案。"""
        typed: dict[str, object] = {}
        for key, value in args.items():
            kind = self.manifest.params.get(key, "str")
            if kind == "int":
                try:
                    typed[key] = int(value)
                except ValueError:
                    return f"{key}={value!r} 需 int"
            elif kind == "float":
                try:
                    typed[key] = float(value)
                except ValueError:
                    return f"{key}={value!r} 需 float"
            else:
                typed[key] = value
        return typed

    def _write_raw(self, result: McpCallResult) -> Path:
        """raw 产物唯一生成点：JSON-RPC 结果如实落盘（诚实锚点，同 nmap 手式）。

        文件名里的 server/tool 名先消毒——名字来自外部 server，不信任其字符集。
        """
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        stem = _UNSAFE_PATH_CHARS.sub("_", f"{self._client.server_name}_{self._tool.name}")
        path = self._raw_dir / f"mcp_{stem}_{uuid.uuid4().hex}.txt"
        path.write_text(result.content_text, encoding="utf-8")
        return path


def build_mcp_adapters(
    configs: Sequence[McpServerConfig], raw_dir: Path | str
) -> list[ToolAdapter]:
    """配置 → adapter 列表；掉线只告警不注册（装配绝不被坏 server 炸）。"""
    adapters: list[ToolAdapter] = []
    for cfg in configs:
        client = McpStdioClient(cfg.name, cfg.command, timeout_s=cfg.timeout_s)
        try:
            tools = client.list_tools()
        except McpError as exc:
            _log.warning("mcp_register_failed", server=cfg.name, error=str(exc))
            continue
        for tool in tools:
            adapters.append(McpTool(client=client, tool=tool, raw_dir=raw_dir))
    return adapters


__all__ = [
    "McpCallResult",
    "McpServerConfig",
    "McpStdioClient",
    "McpTool",
    "McpToolDef",
    "build_mcp_adapters",
    "load_mcp_config",
]
