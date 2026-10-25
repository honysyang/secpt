"""工具层（ARCHITECTURE §8 L1）：ToolSpec + REGISTRY + manifest。

S5 约束：工具不碰证据库——invoke 只返回 ProcResult，证据落盘只在
gateway（S6）做；工具范围盲——scope 只在 gateway 判一次；timeout 经
ToolContext 传入，不做模块级全局。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pa.tools.base import ToolContext, ProcResult


@dataclass
class ToolSpec:
    name: str
    risk_tier: str              # low | med | high | critical
    side_effect: str            # none | read | write | destructive
    requires: list[str]         # 前置线索，喂"补前置"
    schema: dict                # JSON Schema，进 prompt
    produces_evidence: bool
    fn: Callable[[dict, ToolContext], ProcResult]
    # 声明驱动范围检查（裁定 2）：网络类工具为 True，缺 target 即
    # missing_target；fs 这类 workspace 本地工具显式 False。gateway
    # 不得用"args 里没有 target/url 就跳过"的参数名嗅探关闭安全检查
    requires_network: bool = True


from pa.tools import fs as _fs          # noqa: E402  （先定义 ToolSpec 再注册）
from pa.tools import http as _http
from pa.tools import probe as _probe
from pa.tools import shell as _shell

REGISTRY: dict[str, ToolSpec] = {
    "shell": ToolSpec(
        name="shell", risk_tier="med", side_effect="read",
        requires=[],
        schema={"type": "object",
                "properties": {"cmd": {"type": "string"}, "target": {"type": "string"}},
                "required": ["cmd"]},
        produces_evidence=True, fn=_shell.invoke),
    "http": ToolSpec(
        name="http", risk_tier="low", side_effect="read",
        requires=[],
        schema={"type": "object",
                "properties": {"method": {"type": "string"}, "url": {"type": "string"},
                               "headers": {"type": "object"},
                               "body": {"type": "string"}},
                "required": ["url"]},
        produces_evidence=True, fn=_http.invoke),
    "fs": ToolSpec(
        name="fs", risk_tier="low", side_effect="read",
        requires=[],
        schema={"type": "object",
                "properties": {"op": {"enum": ["read", "list"]},
                               "path": {"type": "string"}},
                "required": ["op", "path"]},
        produces_evidence=True, fn=_fs.invoke,
        requires_network=False),
    "probe": ToolSpec(
        name="probe", risk_tier="low", side_effect="read",
        requires=[],
        schema={"type": "object",
                "properties": {"target": {"type": "string"},
                               "port": {"type": "integer"}},
                "required": ["target", "port"]},
        produces_evidence=True, fn=_probe.invoke),
}


def manifest() -> list[dict]:
    """工具清单，进 prompt（schema + 元信息，不带 fn）。"""
    return [{"name": s.name, "risk_tier": s.risk_tier,
             "side_effect": s.side_effect, "requires": s.requires,
             "schema": s.schema, "produces_evidence": s.produces_evidence,
             "requires_network": s.requires_network}
            for s in REGISTRY.values()]
