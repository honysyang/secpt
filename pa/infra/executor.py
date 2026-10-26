"""本地命令执行器：文件重定向（陷阱 A4）+ 进程组超时收割 + 两层截断上限。

- stdout/stderr 重定向到 workdir 下的临时文件，不用 capture_output
  （陷阱 A4：管道死锁 + 大输出内存膨胀）；
- 超时 → killpg(SIGKILL) 整个进程组，子进程不留孤儿（S4 追加验收二）；
- 截断两层上限（裁定 4）：单行 200KB；单条证据总量 2MB——
  读取时按字节上限截断，truncated_at 记 "total" | "line"（总量优先），
  original_bytes 记截断前总字节，证据 meta 一并携带。不加总量上限，
  一次 nmap -A 就能写出几十 MB。
- 重定向临时文件放 workdir（陷阱 A5：运行数据进 workspace，不进系统 /tmp）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from pa.errors import SecptError

MAX_LINE_BYTES = 200 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024


@dataclass
class ProcResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    truncated: bool = False
    truncated_at: str | None = None  # "total" 总量上限 | "line" 单行上限
    original_bytes: int = 0  # 截断前的原始 stdout 总字节数
    # 工具级结构化观测（如 HTTP 响应头/长度/body hash）。只做证据/线索，
    # 不改变 stdout 语义；旧工具不填，零兼容成本。
    meta: dict | None = None


def _truncate_lines(raw: bytes, limit: int) -> tuple[bytes, bool]:
    out: list[bytes] = []
    truncated = False
    for line in raw.splitlines(keepends=True):
        if len(line) > limit:
            truncated = True
            nl = b"\n" if line.endswith(b"\n") else b""
            out.append(line[:limit] + nl)
        else:
            out.append(line)
    return b"".join(out), truncated


def run(cmd: list[str], *, timeout: float, workdir) -> ProcResult:
    """同步执行命令；超时杀整个进程组并保留已产出输出。"""
    workdir = Path(workdir)
    tag = uuid.uuid4().hex[:12]
    out_path = workdir / f".exec-{tag}.out"
    err_path = workdir / f".exec-{tag}.err"
    timed_out = False
    try:
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            try:
                proc = subprocess.Popen(
                    cmd, stdout=fo, stderr=fe, cwd=str(workdir), start_new_session=True
                )
            except OSError as e:
                raise SecptError(f"exec spawn failed: {cmd[0]}: {e}") from e
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)  # 进程组一锅端
                proc.wait()
            rc = proc.returncode
        raw_out = out_path.read_bytes()
        raw_err = err_path.read_bytes()
    finally:
        for p in (out_path, err_path):
            try:
                p.unlink()
            except OSError:
                pass
    raw = raw_out
    truncated_at = None
    if len(raw) > MAX_TOTAL_BYTES:  # 总量上限：读取时截断
        raw = raw[:MAX_TOTAL_BYTES]
        truncated_at = "total"
    stdout, line_hit = _truncate_lines(raw, MAX_LINE_BYTES)
    if line_hit and truncated_at is None:
        truncated_at = "line"
    return ProcResult(
        exit_code=rc,
        stdout=stdout,
        stderr=raw_err,
        timed_out=timed_out,
        truncated=truncated_at is not None,
        truncated_at=truncated_at,
        original_bytes=len(raw_out),
    )
