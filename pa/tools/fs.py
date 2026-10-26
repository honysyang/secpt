"""文件系统工具（BUILD_SPEC §10.3）：read / list / grep，路径必须落在 workspace 内。

路径逃逸检查（BUILD_SPEC §10.3 公式）：Path(p).resolve().is_relative_to(workspace)。
逃逸返回 exit_code=126（blocked），不读文件。write 操作 v0 不开放
（没有调用方；要写得先过 gateway 的证据/产物路径，S6 再议）。
grep（0day 源码审计）：正则 + glob 限定，跳过二进制与超大文件，
输出 `path:lineno: 行`（行截断 200 字符，总匹配数封顶）。
"""

from __future__ import annotations

import re
from pathlib import Path

from pa.tools.base import ToolContext, ProcResult, cap_stdout

_GREP_FILE_BYTES = 512_000  # 单文件参与 grep 的上限
_GREP_MAX_MATCHES = 200  # 总匹配行数上限
_GREP_LINE_CAP = 200  # 单行输出截断


def _resolve(ws: Path, p: str) -> Path | None:
    target = Path(p)
    if not target.is_absolute():
        target = ws / p
    target = target.resolve()
    return target if target.is_relative_to(ws) else None


def _grep(root: Path, args: dict) -> ProcResult:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return ProcResult(126, b"", b"blocked:grep_requires_pattern")
    try:
        rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
    except re.error as e:
        return ProcResult(126, b"", f"blocked:bad_regex:{e}".encode())
    glob = str(args.get("glob") or "*")
    target = Path(args["__target__"])
    files = (
        [target]
        if target.is_file()
        else sorted(p for p in target.rglob(glob) if p.is_file())
    )
    matches: list[str] = []
    truncated = False
    for f in files:
        if len(matches) >= _GREP_MAX_MATCHES:
            truncated = True
            break
        try:
            if f.stat().st_size > _GREP_FILE_BYTES:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:1024]:
            continue  # 二进制跳过
        text = raw.decode("utf-8", "replace")
        rel = f.relative_to(root) if f.is_relative_to(root) else f.name
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matches.append(f"{rel}:{i}: {line.strip()[:_GREP_LINE_CAP]}")
                if len(matches) >= _GREP_MAX_MATCHES:
                    truncated = True
                    break
    body = "\n".join(matches)
    if truncated:
        body += f"\n…［匹配数达上限 {_GREP_MAX_MATCHES}，已截断］"
    out, t, orig = cap_stdout((body + "\n").encode("utf-8") if body else b"")
    return ProcResult(
        0,
        out,
        b"",
        truncated=t or truncated,
        original_bytes=orig,
        meta={"matches": len(matches), "pattern": pattern},
    )


def invoke(args: dict, ctx: ToolContext) -> ProcResult:
    op = str(args.get("op", "read"))
    ws = Path(ctx.workspace).resolve()
    target = _resolve(ws, str(args.get("path", "")))
    if target is None:
        return ProcResult(126, b"", b"blocked:path_escape")
    if op == "grep":
        rc = _grep(ws, {**args, "__target__": target})
        return rc
    if op == "read":
        try:
            raw = target.read_bytes()
        except OSError as e:
            return ProcResult(1, b"", str(e).encode())
        out, truncated, original = cap_stdout(raw)
        return ProcResult(0, out, b"", truncated=truncated, original_bytes=original)
    if op == "list":
        if not target.is_dir():
            return ProcResult(1, b"", b"not a directory")
        names = "\n".join(sorted(p.name for p in target.iterdir()))
        return ProcResult(0, (names + "\n").encode(), b"")
    return ProcResult(126, b"", f"blocked:bad_op:{op}".encode())
