"""工具层地基（ARCHITECTURE §8 L1）：ToolSpec + ToolContext + ProcResult。

放这里而不是 pa.tools.__init__的原因：MCP 动态注册（mcp.py）需要构造
ToolSpec，而 mcp 又由 __init__ 的 REGISTRY 之后接入——若 ToolSpec 留在
__init__，mcp 只能 `from pa.tools import ToolSpec` 造出对包半初始化状态
的顺序依赖。ToolSpec 是纯声明结构、无任何 pa 子模块依赖，做地基名正
言顺；__init__ re-export 保持既有导入路径不变（向后兼容）。

timeout 由调用方（gateway）经 ctx 传入，不做模块级全局（S5 约束 C）；
workspace 限定文件工具的活动范围（fs 路径逃逸检查以此为锚）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pa.infra.executor import MAX_TOTAL_BYTES
from pa.infra.executor import (
    ProcResult as ProcResult,
)  # re-export（shell/fs/tls/http/probe 从这里引）


@dataclass
class ToolSpec:
    name: str
    risk_tier: str  # low | med | high | critical
    side_effect: str  # none | read | write | destructive
    requires: list[str]  # 前置线索，喂"补前置"
    schema: dict  # JSON Schema，进 prompt
    produces_evidence: bool
    fn: Callable[[dict, ToolContext], ProcResult]
    # 声明驱动范围检查（裁定 2）：网络类工具为 True，缺 target 即
    # missing_target；fs 这类 workspace 本地工具显式 False。gateway
    # 不得用"args 里没有 target/url 就跳过"的参数名嗅探关闭安全检查
    requires_network: bool = True


@dataclass
class ToolContext:
    workspace: Path
    timeout: float = 120.0


def cap_stdout(raw: bytes) -> tuple[bytes, bool, int]:
    """读取侧总量上限：单条证据 2MB（裁定 4）。返回 (截断后, 是否截断, 原始字节数)。"""
    if len(raw) > MAX_TOTAL_BYTES:
        return raw[:MAX_TOTAL_BYTES], True, len(raw)
    return raw, False, len(raw)
