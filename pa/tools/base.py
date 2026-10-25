"""工具调用上下文（ARCHITECTURE §8 L1）。

timeout 由调用方（gateway）经 ctx 传入，不做模块级全局（S5 约束 C）；
workspace 限定文件工具的活动范围（fs 路径逃逸检查以此为锚）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pa.infra.executor import MAX_TOTAL_BYTES, ProcResult


@dataclass
class ToolContext:
    workspace: Path
    timeout: float = 120.0


def cap_stdout(raw: bytes) -> tuple[bytes, bool, int]:
    """读取侧总量上限：单条证据 2MB（裁定 4）。返回 (截断后, 是否截断, 原始字节数)。"""
    if len(raw) > MAX_TOTAL_BYTES:
        return raw[:MAX_TOTAL_BYTES], True, len(raw)
    return raw, False, len(raw)
