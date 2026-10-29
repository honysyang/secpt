"""M5-29 工作区工具：write_file / run_command / download 的边界与回执。

全部离线：命令只跑 /bin/echo、/bin/false（本机必有），下载用注入 fetcher。
安全断言优先：argv 直执行（无 shell）、元字符拒绝、写/下载都限制在 work/。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from secpt.tools.workspace import DownloadTool, RunCommandTool, WriteFileTool


class RecordingRunner:
    """记录 argv 的执行器替身：证明"进 subprocess 的是列表，不是 shell 串"。"""

    label = "recording"

    def __init__(self, returncode: int = 0, stdout: str = "ok\n") -> None:
        self.seen: list[list[str]] = []
        self._returncode = returncode
        self._stdout = stdout

    def command(self, argv: list[str]) -> list[str]:
        return list(argv)

    def run(self, argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        self.seen.append(list(argv))
        return subprocess.CompletedProcess(argv, self._returncode, self._stdout, "")


# ---- write_file -------------------------------------------------------------


def test_write_file_lands_in_work_root(tmp_path: Path) -> None:
    tool = WriteFileTool(root=tmp_path)
    out = tool.run({"path": "scripts/probe.py", "content": "print('hi')\n"})

    assert out.exit_code == 0
    assert (tmp_path / "scripts" / "probe.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert out.raw_ref == ""  # 自撰文件不是证据锚


def test_write_file_rejects_escape_and_absolute(tmp_path: Path) -> None:
    tool = WriteFileTool(root=tmp_path / "work")
    escape = tool.run({"path": "../raw/x.txt", "content": "x"})
    assert escape.exit_code is None and "越出" in escape.summary
    absolute = tool.run({"path": "/tmp/x.txt", "content": "x"})
    assert absolute.exit_code is None and "相对路径" in absolute.summary


def test_write_file_rejects_oversize(tmp_path: Path) -> None:
    out = WriteFileTool(root=tmp_path).run({"path": "big.txt", "content": "x" * (256 * 1024 + 1)})
    assert out.exit_code is None and "上限" in out.summary


def test_registry_allows_multiline_script_content(tmp_path: Path) -> None:
    """free_text 豁免：content 多行是脚本常态，registry 不得按控制字符一刀切。"""
    from secpt.tools.registry import ToolRegistry

    reg = ToolRegistry([WriteFileTool(root=tmp_path)])
    out = reg.dispatch("write_file", {"path": "s.py", "content": "line1\nline2\n"})
    assert out.exit_code == 0
    assert (tmp_path / "s.py").read_text(encoding="utf-8") == "line1\nline2\n"


# ---- run_command ------------------------------------------------------------


def test_run_command_executes_argv_without_shell(tmp_path: Path) -> None:
    runner = RecordingRunner()
    tool = RunCommandTool(raw_dir=tmp_path / "raw", runner=runner)
    out = tool.run({"program": "/bin/echo", "args": "hello world"})

    assert out.exit_code == 0
    assert runner.seen == [["/bin/echo", "hello", "world"]]  # 列表直执行
    assert out.raw_ref and Path(out.raw_ref).is_file()
    text = Path(out.raw_ref).read_text(encoding="utf-8")
    assert "--- stdout ---\nok" in text
    assert "--- exit ---\n0" in text


def test_run_command_rejects_shell_metacharacters(tmp_path: Path) -> None:
    """§7 安全表：元字符一律拒绝，绝不做"尽力清洗"（清洗即解释器语义）。"""
    runner = RecordingRunner()
    tool = RunCommandTool(raw_dir=tmp_path, runner=runner)
    out = tool.run({"program": "/bin/echo", "args": "a; rm -rf /"})

    assert out.exit_code is None
    assert "元字符" in out.summary
    assert runner.seen == []  # 零执行


def test_run_command_nonzero_exit_is_receipt(tmp_path: Path) -> None:
    runner = RecordingRunner(returncode=1, stdout="")
    out = RunCommandTool(raw_dir=tmp_path, runner=runner).run({"program": "/bin/false"})
    assert out.exit_code == 1
    assert "exit=1" in out.summary
    assert out.raw_ref


def test_run_command_missing_binary_is_receipt(tmp_path: Path) -> None:
    out = RunCommandTool(raw_dir=tmp_path).run({"program": "/nonexistent/tool"})
    assert out.exit_code is None
    assert "找不到可执行文件" in out.summary
    assert out.raw_ref == ""


# ---- download ---------------------------------------------------------------


def test_download_writes_binary_and_sets_executable_bit(tmp_path: Path) -> None:
    tool = DownloadTool(root=tmp_path)
    tool._fetch = lambda _url: b"#!/bin/sh\necho tool\n"  # type: ignore[method-assign]

    out = tool.run({"url": "https://example.com/tool.sh", "executable": "1"})

    assert out.exit_code == 0
    target = tmp_path / "bin" / "tool.sh"
    assert target.is_file() and target.read_bytes().startswith(b"#!/bin/sh")
    assert target.stat().st_mode & 0o111  # 可执行位已设
    assert out.raw_ref == ""


def test_download_rejects_scheme_and_escape(tmp_path: Path) -> None:
    tool = DownloadTool(root=tmp_path / "work")
    bad_scheme = tool.run({"url": "file:///etc/passwd"})
    assert bad_scheme.exit_code is None and "http/https" in bad_scheme.summary
    escape = tool.run({"url": "https://x/tool", "dest": "../evil"})
    assert escape.exit_code is None and "越出" in escape.summary


def test_download_failure_is_receipt(tmp_path: Path) -> None:
    tool = DownloadTool(root=tmp_path)

    def boom(_url: str) -> bytes:
        raise OSError("网络挂了")

    tool._fetch = boom  # type: ignore[method-assign]
    out = tool.run({"url": "https://x.invalid/tool"})
    assert out.exit_code is None
    assert "E_TOOL_FAIL" in out.summary and "网络挂了" in out.summary


def test_workspace_manifests_are_serial() -> None:
    """并发派单（M5-31 翻转）：三个工作区工具都有副作用且存在隐式顺序依赖
    （先写脚本再执行、先下载再运行），一律不声明 parallel_safe——同批有它们
    就整批串行，"先写后跑"放同一步不再是竞态（实测一轮 write_file 与
    run_command 并发，python 打开了尚未写完的脚本路径）。"""
    for manifest in (WriteFileTool.manifest, RunCommandTool.manifest, DownloadTool.manifest):
        assert not manifest.parallel_safe
        assert not manifest.read_only  # 都有副作用，永不进 R1 重放
