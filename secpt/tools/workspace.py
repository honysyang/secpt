"""智能体工作区工具（M5-29）：write_file / run_command / download。

为什么需要：智能体要"自己写脚本、下载工具"才能把一次性动作固化成可复用资产——
此前只有 nmap + 三个只读工具，脚本无处落盘、外部工具无处安放，模型只能反复
点名不存在的 bash/shell（实测 54 张小票里 49 张是"未知工具"）。

安全边界（三条硬纪律）：
- ``write_file`` 只写 ``work/`` 根内：**绝不碰 raw/evidence**——证据目录只能由
  工具产物写，否则模型能自撰"证据"绕过 R0 判零；
- ``run_command`` 吃 argv 列表（shlex 解析），拒绝 shell 元字符，绝不 shell=True；
  走注入的 ArgvRunner（隔离档 docker 同权），超时/输出上限硬编码；
- ``download`` 只认 http/https、32MB 上限、落 ``work/bin/``，可选 chmod +x。

分层纪律：本模块只署回执（exit_code/summary/raw_ref），不落票、不碰账本；
schema 校验、Guard 过闸、探针窗口仍由 registry 统一前置。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from pathlib import Path

from secpt.tools.executors import ArgvRunner, LocalRunner
from secpt.tools.registry import ToolManifest, ToolOutput

_USER_AGENT = "secpt/0.1 (agent workspace)"
_MAX_WRITE = 256 * 1024
_MAX_OUTPUT = 200 * 1024
_MAX_DOWNLOAD = 32 * 1024 * 1024
_DEFAULT_TIMEOUT_S = 120.0
# bash 元字符与控制字符：命中即拒绝（argv 直执行，不给解释器任何机会）。
_FORBIDDEN = frozenset(";|&$><`\n\r") | frozenset(chr(code) for code in range(0x20))


def _first_line(text: str, limit: int = 160) -> str:
    """summary 纪律：一行、有界（全文保真交给 raw_ref）。"""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:limit] + ("…" if len(first) > limit else "")


class _Workspace:
    """工作区根边界：相对路径解析、越界拒绝（write/download 共用）。"""

    def __init__(self, root: Path | str = "work") -> None:
        self._root = Path(root)

    def resolve(self, raw: str) -> Path:
        """把相对路径解析到工作区内；越界/绝对路径一律 ValueError。"""
        text = (raw or "").strip()
        if not text:
            raise ValueError("path 为空")
        if Path(text).is_absolute():
            raise ValueError("只接受相对路径（work/ 内定位）")
        base = self._root.resolve()
        candidate = (base / text).resolve()
        if not candidate.is_relative_to(base):
            raise ValueError("路径越出工作区根（只允许 work/ 内写入）")
        return candidate


class WriteFileTool:
    """写文本文件到工作区（脚本/配置/备注），不产生证据锚。"""

    manifest = ToolManifest(
        name="write_file",
        kind="mcp",
        description="把文本写入 work/ 目录（写脚本/配置用；不产生证据锚）",
        params={"path": "str", "content": "str"},
        required=("path", "content"),
        # 写操作：永不进 R1 重放（未声明 read_only 即写操作，默认 False）。
        # M5-31 也不再声明 parallel_safe：同批"先写后跑"的依赖顺序不被并发保证
        # （实测一轮 write_file + run_command 同步并发，python 打开了还没写完的
        # 脚本路径）——有副作用的工具一律串行，"未知即保守"落到实处。
        # 正文天生多行：只有 content 豁免控制字符检查（脚本写不出来就别谈"自己写脚本"）。
        free_text=("content",),
    )

    def __init__(self, root: Path | str = "work") -> None:
        self._workspace = _Workspace(root)

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        content = args.get("content") or ""
        if len(content.encode("utf-8")) > _MAX_WRITE:
            return self._fail(f"E_TOOL_FAIL: 内容超过 {_MAX_WRITE} 字节上限", args)
        try:
            target = self._workspace.resolve(args.get("path") or "")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as exc:
            return self._fail(f"E_TOOL_FAIL: write_file {exc}", args)
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary=f"写入 {args.get('path')}（{len(content.encode('utf-8'))} 字节）",
            raw_ref="",  # 自撰文件不是证据锚（防止口头证据绕过判零）
        )

    def _fail(self, summary: str, args: Mapping[str, str]) -> ToolOutput:
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=None,
            summary=summary,
            raw_ref="",
        )


class RunCommandTool:
    """执行一条命令（argv 列表，禁 shell），stdout/stderr 落 raw 作为证据。"""

    manifest = ToolManifest(
        name="run_command",
        kind="mcp",
        description="按 argv 列表执行命令（禁 shell/管道），输出落 raw 证据；可跑自写脚本",
        params={"program": "str", "args": "str", "timeout_s": "int"},
        required=("program",),
        # M5-31 不声明 parallel_safe：命令是最大号的副作用（读写任意文件、网络），
        # 与同批工具的先后顺序可能就是正确性本身（先写脚本再执行）；默认 False
        # 整批串行，顺序 = 调用序，依赖动作放同一步也不再是竞态。
    )

    def __init__(
        self,
        raw_dir: Path | str = "raw",
        *,
        runner: ArgvRunner | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._raw_dir = Path(raw_dir)
        self._runner = runner if runner is not None else LocalRunner()
        self._timeout_s = timeout_s

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        program = (args.get("program") or "").strip()
        raw_args = args.get("args") or ""
        problem = self._check_tokens(program, raw_args)
        if problem is not None:
            return self._fail(problem, args)
        try:
            extra = shlex.split(raw_args)
        except ValueError as exc:
            return self._fail(f"E_TOOL_FAIL: args 无法解析（{exc}）", args)
        argv = [program, *extra]
        timeout_s = _clamp_timeout(args.get("timeout_s"), self._timeout_s)
        try:
            proc = self._runner.run(argv, timeout_s=timeout_s)
        except FileNotFoundError:
            return self._fail(f"E_TOOL_FAIL: 找不到可执行文件 {program}", args)
        except subprocess.TimeoutExpired:
            return self._fail(f"E_TOOL_FAIL: 超时 {timeout_s:g}s", args)
        except OSError as exc:
            return self._fail(f"E_TOOL_FAIL: 执行失败 {type(exc).__name__}: {exc}", args)
        stdout = (proc.stdout or "")[:_MAX_OUTPUT]
        stderr = (proc.stderr or "")[:_MAX_OUTPUT]
        raw_path = self._write_raw(
            self._runner.command(argv), stdout, stderr, proc.returncode, cwd=os.getcwd()
        )
        excerpt = _first_line(stdout) or _first_line(stderr) or "（无输出）"
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=proc.returncode,
            summary=f"{excerpt}（exit={proc.returncode}）",
            raw_ref=str(raw_path),
        )

    def _check_tokens(self, program: str, raw_args: str) -> str | None:
        """禁 shell 注入（§7 安全表）：元字符/控制字符一律拒，不做"尽力清洗"。"""
        if not program:
            return "E_TOOL_FAIL: program 为空"
        for text in (program, raw_args):
            bad = sorted({char for char in text if char in _FORBIDDEN})
            if bad:
                return f"E_TOOL_FAIL: 含 shell 元字符/控制字符 {bad}（只接受 argv 列表语义）"
        return None

    def _fail(self, summary: str, args: Mapping[str, str]) -> ToolOutput:
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=None,
            summary=summary,
            raw_ref="",
        )

    def _write_raw(
        self, argv: list[str], stdout: str, stderr: str, exit_code: int, *, cwd: str
    ) -> Path:
        """raw 产物唯一生成点：指针必须指向刚写出的真文件（诚实锚点）。

        M5-31 封面带 cwd：相对路径的脚本/产物"在哪找"从此可复算——真实 run 实测
        write_file 落盘路径与 run_command 工作目录口径不一致，模型连败一步才悟出原因。
        """
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        path = self._raw_dir / f"exec_{uuid.uuid4().hex}.txt"
        path.write_text(
            f"$ argv({' '.join(argv)})\n$ cwd({cwd})\n\n--- exit ---\n{exit_code}\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
            encoding="utf-8",
        )
        return path


def _clamp_timeout(raw: str | None, default: float) -> float:
    """timeout 解析：非法/越界回落默认，并夹在 1..600s（不许"永远不超时"）。"""
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return max(1.0, min(value, 600.0))


class DownloadTool:
    """下载外部工具/字典到 work/bin/（可选 chmod +x），只认 http/https。"""

    manifest = ToolManifest(
        name="download",
        kind="mcp",
        description="下载 http/https 文件到 work/bin/（工具/字典；可选加可执行位）",
        params={"url": "str", "dest": "str", "executable": "int"},
        required=("url",),
        # M5-31 不声明 parallel_safe：落盘 work/ 的写副作用与"下载完就执行"的
        # 顺序依赖同上，默认保守串行。
    )

    def __init__(self, root: Path | str = "work", *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._workspace = _Workspace(root)
        self._timeout_s = timeout_s

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        url = (args.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return self._fail(f"E_TOOL_FAIL: download 只接受 http/https URL，实得 {url!r}", args)
        dest = (args.get("dest") or "").strip() or f"bin/{_basename(url)}"
        try:
            target = self._workspace.resolve(dest)
        except ValueError as exc:
            return self._fail(f"E_TOOL_FAIL: download {exc}", args)
        try:
            body = self._fetch(url)
        except Exception as exc:  # noqa: BLE001  网络失败一律回执
            return self._fail(f"E_TOOL_FAIL: 下载 {url} 失败 {type(exc).__name__}: {exc}", args)
        if len(body) > _MAX_DOWNLOAD:
            return self._fail(f"E_TOOL_FAIL: 文件超过 {_MAX_DOWNLOAD} 字节上限", args)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            if (args.get("executable") or "0").strip() in ("1", "true", "yes"):
                target.chmod(0o755)
        except OSError as exc:
            return self._fail(f"E_TOOL_FAIL: 落盘失败 {exc}", args)
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary=f"下载 {url} → {dest}（{len(body)} 字节）",
            raw_ref="",  # 二进制不是文本证据，不冒充锚点
        )

    def _fetch(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310
                return response.read(_MAX_DOWNLOAD + 1)
        except urllib.error.HTTPError as exc:
            raise OSError(f"HTTP {exc.code}") from exc

    def _fail(self, summary: str, args: Mapping[str, str]) -> ToolOutput:
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=None,
            summary=summary,
            raw_ref="",
        )


def _basename(url: str) -> str:
    """从 URL 推一个安全文件名（去掉查询串/分隔符，空则随机名）。"""
    name = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    name = os.path.basename(name).strip()
    return name or f"download_{uuid.uuid4().hex[:8]}"


__all__ = ["DownloadTool", "RunCommandTool", "WriteFileTool"]
