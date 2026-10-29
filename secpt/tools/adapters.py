"""外部工具适配器（M1 唯一外部工具 = nmap）与内嵌工具 agent_run 占位。

为什么 nmap 在这而不是 MCP：§3.5 工具库 M1 状态 = 本地适配器起手（MCP 管道 M2
接线）——manifest+hash 同一张注册表，kind=mcp 的对外语义不变，命令一律 argv
列表执行、禁 shell=True（§7 安全表），拒绝控制字符的闸门在 registry 层。

raw_ref 诚实原则：raw 指针必须指向真实存在的文件——每次执行把 stdout/stderr
落盘 ``raw_dir``，绝不返回空指针或假路径。
"""

from __future__ import annotations

import re
import subprocess
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import Future
from pathlib import Path
from typing import cast

from secpt.core.mission_board import Mission, MissionBoard, MissionResult
from secpt.tools.broker import ApprovalBroker
from secpt.tools.executors import ArgvRunner, LocalRunner
from secpt.tools.registry import ToolManifest, ToolOutput

# 主循环收拢 worker 小票的轮询步长：等待窗内顺手 serve 审批代理队列。
_SERVE_POLL_S = 0.2
# 收拢默认超时（M5-34）：worker 炸死/LLM 悬挂时主循环不能陪葬——超时转
# E_TOOL_FAIL 小票返回，worker 侧工单继续自生自灭（audit 有痕）。
_DEFAULT_MISSION_TIMEOUT_S = 600.0


def _single_line(text: str, limit: int = 160) -> str:
    """summary 纪律：一行、有界（喂注意力包的紧凑投影）。

    只对"跨行"归一（多个段落压成一行），不折叠行内空格——nmap 等工具的
    对齐输出里 `22/tcp open  ssh` 的双空格是原文，丢掉就背叛了锚点。
    全文保真由 raw_ref 负责，summary 只做换行级压缩。
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    single = first.replace("\t", " ")[:limit]
    return single + ("…" if len(first) > limit else "")


_PORT_LINE = re.compile(r"^\s*\d+/(?:tcp|udp)\s+\S+", re.IGNORECASE)


def _port_digest(stdout: str, limit: int = 3) -> str:
    """nmap summary 优先给端口结论（M5-30）。

    为什么：stdout 首行是 ``Starting Nmap 7.95 … at …``（启动 banner），模型从
    摘要里根本看不到 `22/tcp open ssh`，只能再去 read_file——这正是上一轮 98 张
    read_file 票的主要推力。端口行才是这一步的结论，banner 只是噪音。
    """
    ports = [line.strip() for line in stdout.splitlines() if _PORT_LINE.match(line)]
    return "；".join(ports[:limit])


class NmapTool:
    """端口/服务扫描适配器：argv 序 = [binary, -Pn, -p, ports, target]。"""

    manifest = ToolManifest(
        name="nmap",
        kind="mcp",
        description="nmap 端口与服务扫描（-sV 恒开）",
        params={"target": "str", "ports": "str"},
        required=("target",),
        # 只读声明（M3-4 拍板①）：nmap 扫描无副作用，R1 重放可原样复跑；
        # agent_run/declare_finding 不声明 = 写操作，永不进重放（默认 False）。
        read_only=True,
        # nmap 是独立子进程 + 独立 raw 文件：可与同批其它扫描并发（M5-12）。
        parallel_safe=True,
    )

    def __init__(
        self,
        binary: str | Path = "/usr/bin/nmap",
        raw_dir: Path | str = "raw",
        timeout_s: float = 120.0,
        runner: ArgvRunner | None = None,
    ) -> None:
        """runner 是 M3-5 执行隔离注入点：None = LocalRunner（M1/M2 行为守恒），
        DockerRunner = 隔离执行；失败文案带 runner.label，让"谁没跑成"可证。"""
        self._binary = str(binary)
        self._raw_dir = Path(raw_dir)
        self._timeout_s = timeout_s
        self._runner = runner

    def build_argv(self, target: str, ports: str | None) -> list[str]:
        """argv 构建独立成纯函数：可测注入面，无 subprocess 无副作用。"""
        argv = [self._binary, "-Pn", "-sV"]
        if ports:
            argv += ["-p", ports]
        argv.append(target)
        return argv

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        """执行 nmap 并落 raw 产物；失败/超时一律转回执（E_TOOL_FAIL 语义）。"""
        argv = self.build_argv(args.get("target", ""), args.get("ports") or None)
        active = self._runner if self._runner is not None else LocalRunner()
        try:
            proc = active.run(argv, timeout_s=self._timeout_s)
        except FileNotFoundError as exc:
            missing = exc.filename or self._binary
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: 执行器 {active.label} 找不到二进制 {missing}",
                raw_ref="",
            )
        except subprocess.TimeoutExpired:
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: 超停 {self._timeout_s}s",
                raw_ref="",
            )

        raw_path = self._write_raw(active.command(argv), proc.stdout, proc.stderr)
        exit_code = proc.returncode or 0
        digest = (
            _port_digest(proc.stdout)
            or _single_line(proc.stdout.strip() if proc.stdout.strip() else proc.stderr.strip())
            or f"exit={exit_code}"
        )
        # M5-33 摘要带证据锚路径（与 http_get 同式）：注意力包只有 summary 一条
        # 通道，nmap 摘要缺路径时模型看不见证据文件名——实测一轮 13 次 list_dir、
        # 5 步空转都在反查"nmap 证据到底叫什么"。
        summary = f"{digest} → {raw_path}"
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=exit_code,
            summary=summary,
            raw_ref=str(raw_path),
        )

    def _write_raw(self, argv: list[str], stdout: str, stderr: str) -> Path:
        """raw 产物唯一生成点：指针必须指向刚写出的真文件（诚实锚点）。"""
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        path = self._raw_dir / f"nmap_{uuid.uuid4().hex}.txt"
        path.write_text(
            f"$ argv({' '.join(argv)})\n\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
            encoding="utf-8",
        )
        return path


class AgentRunTool:
    """内嵌工具 agent_run（§3.6）：主循环派单的收拢窗口。

    M1 占位（board=None）逐字节保留——204 例兼容机位，改即破约；M3-5 整装版
    （board 非 None）：Mission 构造 → Board.submit → future.result() 阻塞收单，
    回出任一小票 `agent_run 收拢：{worker 摘要}`（exit_code 0/1 如实署名）。
    M4-4 起收拢等待窗内代问审批：broker 非 None 时轮询 serve 代理队列——
    worker 只经 agent_run 派生，主线程此刻必然阻塞在本 future 上，单线程
    通道就够（CLI prompt 档由主循环代问，任务书口径）。
    派单/收单链路任何异常（无空板、worker 引爆）一律转 E_TOOL_FAIL 小票——
    与 nmap 失败同手式：不吹牛、不炸主循环，异常原文进 summary 留痕。
    """

    manifest = ToolManifest(
        name="agent_run",
        kind="in-process",
        # M5-33 描述写明"可一步多单并发"——机械能力早就在，模型不知道等于没有。
        description="Sub-agent 领单（任务板派单；可一步多单并发，未装配板回占位小票）",
        params={
            "mission": "str",
            "persona": "str",
            "step_budget": "int",
            "skills": "str",
            "coach_after_failures": "int",
        },
        required=("mission", "persona", "step_budget"),
        # 任务板本身是多线程队列：同一批多张单并发提交即并行领干（M5-12）。
        parallel_safe=True,
    )

    def __init__(
        self,
        board: MissionBoard | None = None,
        broker: ApprovalBroker | None = None,
        *,
        mission_timeout_s: float = _DEFAULT_MISSION_TIMEOUT_S,
    ) -> None:
        """board 是 M3-5 接线点、broker 是 M4-4 接线点：None=占位/无代问守恒。

        ``mission_timeout_s``（M5-34）：收拢等待的硬上限——worker 线程炸死/
        LLM 悬挂时主循环不能陪葬；超时转 E_TOOL_FAIL 小票，worker 侧继续
        自生自灭（其 audit/findings 由它自己的 conn 落账，账不丢）。
        """
        self._board = board
        self._broker = broker
        self._mission_timeout_s = mission_timeout_s

    def _placeholder(self, args: Mapping[str, str]) -> ToolOutput:
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=None,
            summary="E_TOOL_FAIL: agent_run M1 占位——worker 执行器 M2 接线",
            raw_ref="",
        )

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        if self._board is None:
            return self._placeholder(args)
        try:
            mission = Mission(
                persona=(args.get("persona") or "worker").strip() or "worker",
                goal=(args.get("mission") or "").strip() or "(空工单)",
                step_budget=int(args.get("step_budget") or 8),
                # skills 用逗号分隔的 id 清单（manifest 只认 str 类型参数）；
                # 空白项丢弃——空技能 id 不该进工单，也不该报错打断派单。
                skills=tuple(
                    item.strip() for item in (args.get("skills") or "").split(",") if item.strip()
                ),
                # 教练接入门槛（M5-13）：缺省 0 = 不接入，保持 M3-5 老语义。
                coach_after_failures=int(args.get("coach_after_failures") or 0),
            )
            future = self._board.submit(mission)
            result = self._collect(future)
        except Exception as exc:  # 派单/执行全链：退款小票，不炸主循环
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: agent_run 派单失败 {type(exc).__name__}: {exc}",
                raw_ref="",
            )
        if result is None:
            # M5-34 收拢超时：worker 可能仍在执行（其 audit/findings 由它自己的
            # 连接落账，账不丢），主循环拿到诚实的小票继续走，绝不陪葬悬挂。
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=(
                    f"E_TOOL_FAIL: agent_run 收拢超时（{self._mission_timeout_s:g}s）——"
                    "worker 可能仍在执行，其 audit/findings 照常落账"
                ),
                raw_ref="",
            )
        finished = result.ok and result.finished
        summary = f"agent_run 收拢：{result.summary}"
        if result.ok and not result.finished:
            # 未完工要写在摘要里（M5-27）：主循环只看得到 summary，不标清楚的话
            # "预算用尽、零申报"读起来和"干完了"没区别。
            summary += "（未完工）"
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0 if result.ok else 1,
            summary=summary,
            raw_ref="",
            # ok=True/exit=0 只说明"没炸"：未完工同样不算进展，交回执显式声明，
            # 由主循环的失速计按"无进展"收数（M5-27）。
            made_progress=finished,
        )

    def _collect(self, future: Future[object]) -> MissionResult | None:
        """收单：有代理时在等待窗内代问（M4-4——CLI prompt 档由主循环代问）。

        为什么代问挂在收拢等待里：worker 只经 agent_run 派生，主线程此刻
        必然阻塞在本 future 上——0.2s 轮询窗里顺手 serve 代理队列，单线程
        通道就够，不需要专职服务员线程。
        M5-34：等待有硬上限（``mission_timeout_s``）——worker 炸死/LLM 悬挂
        时主循环不能陪葬，超时返回 None 由 run() 转 E_TOOL_FAIL 小票。
        """
        deadline = time.monotonic() + self._mission_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return cast("MissionResult", future.result(timeout=min(_SERVE_POLL_S, remaining)))
            except TimeoutError:  # concurrent.futures 超时（3.11+ 即内建本尊）
                if self._broker is not None:
                    self._broker.serve_once()


class DeclareFindingTool:
    """内嵌工具 declare_finding（W3-2 拍板①）：claim 的正门入口。

    为什么 run 不写账（G 方案纪律）：findings 唯一写口在 store/ledger，本工具
    只署回执——主循环在 step_transaction 里看到"受理"小票才原子落账，保证
    finding 与小票同生死（回执在、账在；小票丢了账也不会独活）。
    空白锚在工具口就拒收（不受理不落 finding）：空白声明的失败小票本身就是
    痕，R0 再兜"文件不存在/零字节"在 sweep 层——两级拦 Different failure。
    """

    manifest = ToolManifest(
        name="declare_finding",
        kind="in-process",
        description="申报 finding（evidence_ref 必须是已在档的非空证据文件）",
        params={"title": "str", "detail": "str", "evidence_ref": "str"},
        required=("title", "detail", "evidence_ref"),
    )

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        ref = (args.get("evidence_ref") or "").strip()
        if not ref:
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary="E_TOOL_FAIL: evidence_ref 为空，申报不受理（无凭裁不立项）",
                raw_ref="",
            )
        title = (args.get("title") or "").strip()
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary=f"finding 受理：{title or '(无题)'}（落账与裁决由主循环收口）",
            raw_ref=ref,
        )


__all__ = ["AgentRunTool", "DeclareFindingTool", "NmapTool"]
