"""工具层（ARCHITECTURE §8 L1）：ToolSpec + REGISTRY + manifest。

S5 约束：工具不碰证据库——invoke 只返回 ProcResult，证据落盘只在
gateway（S6）做；工具范围盲——scope 只在 gateway 判一次；timeout 经
ToolContext 传入，不做模块级全局。
"""

from __future__ import annotations

from pa.tools.base import ToolContext, ProcResult, ToolSpec  # noqa: F401

# re-export（ToolSpec 定义在 base.py，见其模块 docstring——避免 mcp.py
# 对本包半初始化状态的顺序依赖）；外部照旧 `from pa.tools import ToolSpec`。

from pa.tools import dns as _dns  # noqa: E402
from pa.tools import fs as _fs  # noqa: E402  （先定义 ToolSpec 再注册）
from pa.tools import http as _http  # noqa: E402
from pa.tools import probe as _probe  # noqa: E402
from pa.tools import shell as _shell  # noqa: E402
from pa.tools import tls as _tls  # noqa: E402

REGISTRY: dict[str, ToolSpec] = {
    "dns": ToolSpec(
        name="dns",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
        produces_evidence=True,
        fn=_dns.invoke,
    ),
    "shell": ToolSpec(
        name="shell",
        risk_tier="med",
        side_effect="read",
        requires=[],
        schema={
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "target": {"type": "string"}},
            "required": ["cmd"],
        },
        produces_evidence=True,
        fn=_shell.invoke,
    ),
    "http": ToolSpec(
        name="http",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={
            "type": "object",
            "properties": {
                "method": {"type": "string"},
                "url": {"type": "string"},
                "headers": {"type": "object"},
                "body": {"type": "string"},
            },
            "required": ["url"],
        },
        produces_evidence=True,
        fn=_http.invoke,
    ),
    "fs": ToolSpec(
        name="fs",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={
            "type": "object",
            "properties": {
                "op": {"enum": ["read", "list", "grep"]},
                "path": {"type": "string"},
                "pattern": {"type": "string", "description": "grep 正则"},
                "glob": {"type": "string", "description": "grep 文件过滤，如 *.py"},
                "ignore_case": {"type": "boolean"},
            },
            "required": ["op", "path"],
        },
        produces_evidence=True,
        fn=_fs.invoke,
        requires_network=False,
    ),
    "probe": ToolSpec(
        name="probe",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={
            "type": "object",
            "properties": {"target": {"type": "string"}, "port": {"type": "integer"}},
            "required": ["target", "port"],
        },
        produces_evidence=True,
        fn=_probe.invoke,
    ),
    "tls": ToolSpec(
        name="tls",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={
            "type": "object",
            "properties": {"target": {"type": "string"}, "port": {"type": "integer"}},
            "required": ["target"],
        },
        produces_evidence=True,
        fn=_tls.invoke,
    ),
}


from pa.tools import mcp as _mcp  # noqa: E402

_mcp.register(REGISTRY)


def manifest() -> list[dict]:
    """工具清单，进 prompt（schema + 元信息，不带 fn）。"""
    return [
        {
            "name": s.name,
            "risk_tier": s.risk_tier,
            "side_effect": s.side_effect,
            "requires": s.requires,
            "schema": s.schema,
            "produces_evidence": s.produces_evidence,
            "requires_network": s.requires_network,
        }
        for s in REGISTRY.values()
    ]
