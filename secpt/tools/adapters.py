"""外部工具适配器（M1 唯一外部工具 = nmap）与内嵌工具 agent_run 占位。

为什么 nmap 在这而不是 MCP：§3.5 工具库 M1 状态 = 本地适配器起手（MCP 管道 M2
接线）——manifest+hash 同一张注册表，kind=mcp 的对外语义不变，命令一律 argv
列表执行、禁 shell=True（§7 安全表），拒绝控制字符的闸门在 registry 层。

raw_ref 诚实原则：raw 指针必须指向真实存在的文件——每次执行把 stdout/stderr
落盘 ``raw_dir``，绝不返回空指针或假路径。
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Mapping
from pathlib import Path

from secpt.tools.registry import ToolManifest, ToolOutput


def _single_line(text: str, limit: int = 160) -> str:
    """summary 纪律：一行、有界（喂注意力包的紧凑投影）。

    只对"跨行"归一（多个段落压成一行），不折叠行内空格——nmap 等工具的
    对齐输出里 `22/tcp open  ssh` 的双空格是原文，丢掉就背叛了锚点。
    全文保真由 raw_ref 负责，summary 只做换行级压缩。
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    single = first.replace("\t", " ")[:limit]
    return single + ("…" if len(first) > limit else "")


class NmapTool:
    """端口/服务扫描适配器：argv 序 = [binary, -Pn, -p, ports, target]。"""

    manifest = ToolManifest(
        name="nmap",
        kind="mcp",
        description="nmap 端口与服务扫描（-sV 恒开）",
        params={"target": "str", "ports": "str"},
        required=("target",),
    )

    def __init__(
        self,
        binary: str | Path = "/usr/bin/nmap",
        raw_dir: Path | str = "raw",
        timeout_s: float = 120.0,
    ) -> None:
        self._binary = str(binary)
        self._raw_dir = Path(raw_dir)
        self._timeout_s = timeout_s

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
        try:
            proc = subprocess.run(  # noqa: S603  # argv 列表，无 shell
                argv, capture_output=True, text=True, timeout=self._timeout_s, check=False
            )
        except FileNotFoundError:
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: 找不到二进制 {self._binary}",
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

        raw_path = self._write_raw(argv, proc.stdout, proc.stderr)
        exit_code = proc.returncode or 0
        summary = (
            _single_line(proc.stdout.strip() if proc.stdout.strip() else proc.stderr.strip())
            or f"exit={exit_code}"
        )
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
    """内嵌工具 agent_run（§3.6）：kind=in-process 的登记占位。

    M1 只立登记与禁递归机制——worker 执行器（Mission/角色/预算内循环）是 M2
    的器官施工件，这里绝不假实现；如果 M1 版主 LLM 在 tool_calls 里点名
    agent_run，回执明说“占位”，落小票不吹牛。
    """

    manifest = ToolManifest(
        name="agent_run",
        kind="in-process",
        description="Sub-agent 领单（M1 登记占位，执行器 M2 接线）",
        params={"mission": "str", "persona": "str", "step_budget": "int"},
        required=("mission", "persona", "step_budget"),
    )

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=None,
            summary="E_TOOL_FAIL: agent_run M1 占位——worker 执行器 M2 接线",
            raw_ref="",
        )


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
