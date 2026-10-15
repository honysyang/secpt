"""analyze_binary：二进制静态分析 wrapper（只读，Python 插件形态）。

聚合常用静态分析命令为一次 JSON 调用，供 LLM 快速建立二进制画像：
  sections  — objdump -h（节区/权限/大小）
  symbols   — objdump -t（符号表）
  strings   — strings -n <min>（可打印字符串线索）
  gadgets   — ROPgadget --only "pop|ret"（ROP gadget 摘要，工具缺失降级）

安全边界：
- 全部命令只读；执行经 local_runner.run_local（argv 数组 + rlimit + env 最小化 + 审计）
- 截断摘要返回给模型（每类 N 条），完整 JSON 落 <work_dir>/outputs/analyze-<name>.json
- path 必须指向已存在的普通文件；禁止目录/设备文件
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from secpt.runtime.local_runner import run_local

_SECTIONS_CAP = 40
_SYMBOLS_CAP = 80
_STRINGS_CAP = 60
_GADGETS_CAP = 40


def _run(argv: list[str], timeout_s: int = 60) -> str:
    """跑单个只读命令；输出含 local_runner 头行，剥离后只取正文。"""
    out = run_local(argv, timeout_s=timeout_s, cpu_s=30, mem_mb=512)
    # 头行格式：[local_runner] rc=... 完整输出=...\n<正文>
    lines = out.split("\n", 1)
    body = lines[1] if len(lines) > 1 else ""
    if body.startswith("Error:") or out.startswith("Error:"):
        return ""
    return body


def _parse_sections(text: str) -> list[dict]:
    """objdump -h 输出 → [{name, size, vma}]（节区行：Idx Name Size VMA LMA ...）。"""
    rows: list[dict] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 7 and parts[0].isdigit():
            rows.append({"name": parts[1], "size": parts[2], "vma": parts[3]})
    return rows[:_SECTIONS_CAP]


def _parse_symbols(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()][:_SYMBOLS_CAP]


def _parse_gadgets(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ":" in ln][:_GADGETS_CAP]


def analyze(path: str, strings_min: int = 6) -> dict:
    """执行全部分析命令，返回聚合 dict（不存在的外部工具对应键降级为空）。"""
    p = Path(path)
    result: dict = {
        "path": str(p),
        "sections": [],
        "symbols": [],
        "strings": [],
        "rop_gadgets": [],
        "missing_tools": [],
    }
    out = _run(["objdump", "-h", path])
    if out:
        result["sections"] = _parse_sections(out)
    else:
        result["missing_tools"].append("objdump")

    out = _run(["objdump", "-t", path])
    if out:
        result["symbols"] = _parse_symbols(out)

    out = _run(["strings", "-n", str(max(4, int(strings_min))), path], timeout_s=90)
    if out:
        result["strings"] = [ln for ln in out.splitlines() if ln.strip()][:_STRINGS_CAP]
    else:
        result["missing_tools"].append("strings")

    out = _run(["ROPgadget", "--binary", path, "--only", "pop|ret"], timeout_s=120)
    if out:
        result["rop_gadgets"] = _parse_gadgets(out)
    else:
        result["missing_tools"].append("ROPgadget")
    return result


async def _fn(args: dict) -> str:
    args = args or {}
    path = str(args.get("path") or "")
    if not path or os.path.isdir(path) or not os.path.isfile(path):
        return f"Error: path 必须是已存在的文件: {path!r}"

    strings_min = int(args.get("strings_min") or 6)
    data = await asyncio.to_thread(analyze, path, strings_min)

    # 完整结果落盘；截断摘要返回模型
    work_dir = Path(os.environ.get("SECPT_WORK_DIR", Path.cwd()))
    out_dir = work_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    full_path = out_dir / f"analyze-{Path(path).name}.json"
    full_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = {
        "path": data["path"],
        "sections": data["sections"],
        "symbols": data["symbols"][:30],
        "strings": data["strings"][:30],
        "rop_gadgets": data["rop_gadgets"][:20],
        "missing_tools": data["missing_tools"],
        "full_json": str(full_path),
    }
    return json.dumps(summary, ensure_ascii=False, indent=1)


_SCHEMA = {
    "description": (
        "二进制静态分析（只读）：一次返回 sections/symbols/strings/rop_gadgets 摘要 JSON，"
        "完整结果落 outputs/analyze-<名>.json。用于 pwn 前置画像（checksec 的补充）。"
    ),
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "二进制文件路径（相对 work_dir 或绝对路径）"},
        "strings_min": {"type": "integer", "description": "strings 最小长度（默认 6）"},
    },
    "required": ["path"],
}


def register() -> dict[str, tuple[dict, object]]:
    return {"analyze_binary": (_SCHEMA, _fn)}


__all__ = ["analyze", "register"]
