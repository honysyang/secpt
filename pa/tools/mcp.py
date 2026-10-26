"""M5 MCP 工具池：把外部 MCP server 声明为 secpt 工具（数据驱动）。

设计决策（为什么这样做）：

- 每个 MCP server 声明为一个独立 ToolSpec（名字 ``mcp_<name>``）——
  risk_tier / requires_network / side_effect 按 server 粒度声明，gateway
  的 8 步门（存在性→白名单→范围→风险→去重→执行→证据→审计）按现有
  语义原样生效，MCP 不是旁路，是 REGISTRY 里的普通条目（WORKFLOW §1-3
  约束：任何新能力都必须从这两道护栏里过）。
- 声明来自 SECPT_MCP_JSON（envs.mcp_json()，内联 JSON 或文件路径）。
  未配置 = 静默不注册 = 能力缺席（安全缺省）；配置里的坏条目逐条跳过
  降级——坏一个 server 不能炸掉整个工具层，也不能阻断渗透流程。
- 传输：一次性 stdio JSON-RPC 会话（initialize → notifications/initialized
  → tools/call），每次 fn 调用 spawn 一个子进程、会话完即收割。为什么不
  做常驻会话缓存：fn 契约是无状态 (args, ctx) -> ProcResult，常驻缓存
  必然引入模块级可变状态（工程红线：避免全局可变状态）；渗透场景单次
  调用本身就是秒级~分钟级的 nmap/http 重活，进程启动开销占比可忽略。
- 失败一律降级为非零 exit 的 ProcResult，绝不向外抛异常穿透 gateway
  安全门（和 executor.run 的契约一致：工具层不抛，证据层照落）。
- 子进程 stderr 直接写 workspace 临时文件而非管道——stderr 管道无人
  读取时 64KB 缓冲写满会死锁（陷阱 A4，同 executor.run 的处理）。

声明格式（数组或 {"servers": [...]} 均可，字段校验失败整条跳过）::

    [
      {
        "name": "recon",                     # 规则 [a-z][a-z0-9_-]*
        "cmd": ["python", "-m", "recon_mcp"],  # argv 列表，绝不拼 shell
        "risk_tier": "med",                  # 缺省 med（med 是保守中间档）
        "side_effect": "read",               # 缺省 read
        "requires_network": true,            # 缺省 True（走范围检查）
        "tools": ["port_scan", "web_crawl"]  # 可选；供 prompt enum 提示
      }
    ]

调用约定：args 里 ``mcp_tool`` 选 server 内的工具，``timeout`` 留给
gateway（进 ctx），其余顶层参数原样透传给 MCP 工具；提供了显式
``arguments`` 对象时以它为准。target/url/port 留在顶层不是浪费——
gateway 的范围检查正是从这些字段提取 canonical target。
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

from pa import envs
from pa.infra.log import get_logger
from pa.tools.base import ProcResult, ToolContext, ToolSpec, cap_stdout

logger = get_logger("secpt.mcp")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_RISK_TIERS = ("low", "med", "high", "critical")
_SIDE_EFFECTS = ("none", "read", "write", "destructive")

# 留给 secpt 自身的控制键，不透传给 MCP 工具（mcp_tool 是选择器；
# timeout 由 gateway 折进 ctx，透传会被 server 误当业务参数）。
_CONTROL_KEYS = ("mcp_tool", "timeout")
_EXIT_TIMEOUT = 124  # 命令行惯例：超时
_EXIT_DEGRADED = 126  # 惯例：命令不可执行/会话失败


@dataclass
class McpServerSpec:
    """单条 MCP server 声明（manifest 校验后的最终形态）。"""

    name: str
    cmd: list[str]
    risk_tier: str = "med"
    side_effect: str = "read"
    requires_network: bool = True
    tools: list[str] = field(default_factory=list)


def parse_manifest(raw: str) -> list[McpServerSpec]:
    """解析声明 JSON，返回合法条目；坏条目静默跳过（降级哲学）。

    为什么逐条跳过而不是整体失败：mission 中途换声明文件时，一个拼错的
    server 不应让其它 server 的能力凭空消失——逐条校验保证「部分可用
    优于全部拒绝」，同时坏条目名字不进 REGISTRY，不存在半写状态。"""
    try:
        data: Any = json.loads(raw)
    except ValueError as e:
        # 声明整体不可解析 = 用户配置失约，比单条坏更严重，必留痕。
        logger.warning("SECPT_MCP_JSON 不是合法 JSON，MCP 工具池不注册: %s", e)
        return []
    if isinstance(data, dict):
        data = data.get("servers")
    if not isinstance(data, list):
        return []
    out: list[McpServerSpec] = []
    skips: list[str] = []  # 坏条目原因（聚合一次告警，避免逐条刷屏）
    for item in data:
        if not isinstance(item, dict):
            skips.append("条目不是对象")
            continue
        name = item.get("name")
        cmd = item.get("cmd")
        risk = item.get("risk_tier", "med")
        side = item.get("side_effect", "read")
        net = item.get("requires_network", True)
        tools = item.get("tools", [])
        why = ""
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            why = f"name 非法: {name!r}"
        elif (
            not isinstance(cmd, list)
            or not cmd
            or not all(isinstance(c, str) and c for c in cmd)
        ):
            why = f"cmd 非法: {cmd!r}（须非空字符串 argv 列表）"
        elif not isinstance(risk, str) or risk not in _RISK_TIERS:
            why = f"risk_tier 非法: {risk!r}"
        elif not isinstance(side, str) or side not in _SIDE_EFFECTS:
            why = f"side_effect 非法: {side!r}"
        elif not isinstance(net, bool):
            why = f"requires_network 非布尔: {net!r}"
        if why:
            skips.append(f"{name if isinstance(name, str) else '?'}: {why}")
            continue
        if not isinstance(tools, list) or not all(
            isinstance(t, str) and t for t in tools
        ):
            tools = []
        out.append(
            McpServerSpec(
                name=name,
                cmd=list(cmd),
                risk_tier=risk,
                side_effect=side,
                requires_network=net,
                tools=tools,
            )
        )
    if skips:
        # 聚合成一条告警：逐条打会刷屏，静默又丢配置失约现场——一条
        # WARN 级摘要足够运维定位。
        logger.warning("MCP 声明有 %d 条坏条目被跳过: %s", len(skips), "; ".join(skips))
    return out


def _schema(server: McpServerSpec) -> dict[str, Any]:
    """MCP 工具的 args schema：target/url/port 必须留在顶层（gateway
    范围检查的锚点），mcp_tool 是 server 内工具选择器。"""
    sel: dict[str, Any] = {"type": "string"}
    if server.tools:
        sel["enum"] = list(server.tools)
    # 没声明 tools 时选择器不锁 enum——server 能接受任意工具名
    return {
        "type": "object",
        "properties": {
            "mcp_tool": sel,
            "arguments": {
                "type": "object",
                "description": "显式工具参数；缺省时把其余顶层参数原样透传",
            },
            "target": {"type": "string"},
            "url": {"type": "string"},
            "port": {"type": "integer"},
        },
        "required": ["mcp_tool"],
    }


def _tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """敲定透传给 MCP tools/call 的 arguments。

    规则：显式 arguments > 顶层透传（剔除控制键）。不要拆两套语义——
    LLM 给 arguments 就完全信它的对象，不给就把顶层当命名参数转发，
    一条规则可解释、可测试。"""
    explicit = args.get("arguments")
    if isinstance(explicit, dict):
        return dict(explicit)
    return {k: v for k, v in args.items() if k not in _CONTROL_KEYS}


def _run_session(
    cmd: list[str],
    workdir: Path,
    reqs: list[dict[str, Any]],
    answer_id: int,
    timeout: float,
) -> tuple[dict[str, Any] | None, bool]:
    """按给定请求序列跑一次 stdio JSON-RPC 会话，等 answer_id 的响应。

    三个请求一次性写完再关 stdin（server 顺序消费缓冲，EOF 即退出信号，
    免去「握手来回 + 收割」两头竞态）；stdout 用读线程灌队列，主循环
    按 deadline 扫目标 id——阻塞 readline 带 deadline 的唯一轻量解法。
    返回 (响应, 是否超时)；进程崩溃/协议坏行/超时响应为 None。

    stderr 直写临时文件（陷阱 A4：管道死锁），post-mortem 供降级时引用。"""
    payload = b"".join(
        json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n" for r in reqs
    )
    tag = uuid.uuid4().hex[:12]
    err_path = workdir / f".mcp-{tag}.err"
    timed_out = False
    resp: dict[str, Any] | None = None
    reader_out: Any = None
    try:
        with open(err_path, "wb") as fe:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=fe,
                    cwd=str(workdir),
                    start_new_session=True,
                )
            except OSError:
                return None, False

            def _pump() -> None:
                assert reader_out is not None
                try:
                    for line in reader_out:
                        q.put(line)
                finally:
                    q.put(None)

            reader_out = proc.stdout
            q: queue.Queue[bytes | None] = queue.Queue()
            t = threading.Thread(target=_pump, daemon=True)
            t.start()
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
            except OSError:
                pass  # server 提前退出：靠 deadline / EOF 退出循环

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    line = q.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if line is None:
                    break  # EOF：server 退出且没给出目标响应
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # 坏行/MCP 日志混入：跳过继续扫
                if (
                    isinstance(msg, dict)
                    and msg.get("id") == answer_id
                    and ("result" in msg or "error" in msg)
                ):
                    resp = msg
                    break

            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)  # 进程组一锅端
            proc.wait()
    finally:
        if reader_out is not None:
            reader_out.close()
        try:
            err_path.unlink()
        except OSError:
            pass
    return resp, timed_out


def _make_fn(
    server: McpServerSpec,
) -> Callable[[dict[str, Any], ToolContext], ProcResult]:
    """构造 ToolSpec.fn(args, ctx) -> ProcResult。所有失败路径都折成
    非零 exit 的 ProcResult（会话层不抛异常穿透 gateway 安全门）。"""

    def invoke(args: dict[str, Any], ctx: ToolContext) -> ProcResult:
        tool = args.get("mcp_tool")
        if not isinstance(tool, str) or not tool:
            return ProcResult(1, b"", b"mcp: missing mcp_tool selector\n")
        reqs = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": _tool_args(args)},
            },
        ]
        resp, timed_out = _run_session(
            server.cmd, ctx.workspace, reqs, answer_id=2, timeout=ctx.timeout
        )
        if resp is None:
            note = b"mcp server timed out" if timed_out else b"mcp session failed"
            # 会话失败折成非零 exit（证据层照落），但工具层再留一条
            # 告警：渗透运行期一次降级容易被海量步骤日志淹没。
            logger.warning(
                "mcp server %s 调用 %s 会话%s",
                server.name,
                tool or "?",
                "超时" if timed_out else "失败",
            )
            return ProcResult(
                _EXIT_TIMEOUT if timed_out else _EXIT_DEGRADED,
                b"",
                note + b"\n",
                timed_out=timed_out,
            )
        if "error" in resp:
            err = json.dumps(resp["error"], ensure_ascii=False).encode("utf-8")
            return ProcResult(1, b"", err[:4096] + b"\n")
        result = resp.get("result")
        if not isinstance(result, dict):
            return ProcResult(_EXIT_DEGRADED, b"", b"mcp: bad result shape\n")
        if result.get("isError"):
            content = json.dumps(result, ensure_ascii=False).encode("utf-8")
            return ProcResult(1, content[:4096], b"")  # 工具级错误随 stdout 落证据
        text = "".join(
            c.get("text", "")
            for c in result.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        )
        # 证据 cap 同裁定 4（单条 2MB，base.cap_stdout）
        raw, _truncated, _orig = cap_stdout(text.encode("utf-8"))
        return ProcResult(0, raw, b"")

    return invoke


def spec_of(server: McpServerSpec) -> ToolSpec:
    """声明 → ToolSpec（公开给测试直调；register 内部走同一条路）。"""
    return ToolSpec(
        name=f"mcp_{server.name}",
        risk_tier=server.risk_tier,
        side_effect=server.side_effect,
        requires=[],
        schema=_schema(server),
        produces_evidence=True,
        fn=_make_fn(server),
        requires_network=server.requires_network,
    )


def register(registry: dict[str, ToolSpec]) -> int:
    """按 SECPT_MCP_JSON 声明把 mcp_* 工具注进 REGISTRY。返回注册数。

    幂等：重复调用后注册（改名覆盖、同名同构造，语义不变）；env 未
    配置是正常路径（静默 0），不是错误——配置缺席 = 能力缺席。"""
    raw = envs.mcp_json()
    if not raw:
        return 0
    servers = parse_manifest(raw)
    for s in servers:
        registry[f"mcp_{s.name}"] = spec_of(s)
    return len(servers)
