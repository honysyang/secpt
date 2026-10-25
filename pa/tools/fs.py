"""文件系统工具（BUILD_SPEC §10.3）：read / list，路径必须落在 workspace 内。

路径逃逸检查（BUILD_SPEC §10.3 公式）：Path(p).resolve().is_relative_to(workspace)。
逃逸返回 exit_code=126（blocked），不读文件。write 操作 v0 不开放
（没有调用方；要写得先过 gateway 的证据/产物路径，S6 再议）。
"""
from __future__ import annotations

from pathlib import Path

from pa.tools.base import ToolContext, ProcResult, cap_stdout


def _resolve(ws: Path, p: str) -> Path | None:
    target = Path(p)
    if not target.is_absolute():
        target = ws / p
    target = target.resolve()
    return target if target.is_relative_to(ws) else None


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    op = str(args.get("op", "read"))
    ws = Path(ctx.workspace).resolve()
    target = _resolve(ws, str(args.get("path", "")))
    if target is None:
        return ProcResult(126, b"", b"blocked:path_escape")
    if op == "read":
        try:
            raw = target.read_bytes()
        except OSError as e:
            return ProcResult(1, b"", str(e).encode())
        out, truncated, original = cap_stdout(raw)
        return ProcResult(0, out, b"",
                          truncated=truncated, original_bytes=original)
    if op == "list":
        if not target.is_dir():
            return ProcResult(1, b"", b"not a directory")
        names = "\n".join(sorted(p.name for p in target.iterdir()))
        return ProcResult(0, (names + "\n").encode(), b"")
    return ProcResult(126, b"", f"blocked:bad_op:{op}".encode())
