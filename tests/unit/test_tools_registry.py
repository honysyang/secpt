"""tools/registry contract：manifest+hash / argv / 控制字符拒绝 / 禁递归按调用栈。"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from secpt.errors import McpError
from secpt.tools.adapters import AgentRunTool, NmapTool
from secpt.tools.registry import (
    ToolManifest,
    ToolOutput,
    ToolRegistry,
    manifest_hash,
    tool_signature,
)


class EchoLikeTool:
    """可注入假二进制的一次性适配器：真正走 subprocess，exit_code 可控。"""

    manifest = ToolManifest(name="fake", kind="mcp", params={"target": "str"}, required=("target",))

    def __init__(self, binary: Path, raw_dir: Path, timeout_s: float = 10.0) -> None:
        self._binary = str(binary)
        self._raw_dir = raw_dir
        self._timeout_s = timeout_s

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        import subprocess

        try:
            proc = subprocess.run(
                [self._binary],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except FileNotFoundError:
            return ToolOutput(
                tool_name="fake",
                args=dict(args),
                exit_code=None,
                summary="E_TOOL_FAIL: 找不到二进制",
                raw_ref="",
            )
        return ToolOutput(
            tool_name="fake",
            args=dict(args),
            exit_code=proc.returncode,
            summary=f"exit={proc.returncode}",
            raw_ref="",
        )


def _write_script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake_nmap.sh"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---------- manifest + hash ----------


def test_manifest_hash_deterministic_and_sensitive() -> None:
    a = ToolManifest(name="nmap", params={"target": "str"}, required=("target",))
    b = ToolManifest(name="nmap", params={"target": "str"}, required=("target",))
    c = ToolManifest(name="nmap", params={"target": "int"}, required=("target",))
    assert manifest_hash(a) == manifest_hash(b)
    assert manifest_hash(a) != manifest_hash(c)


def test_duplicate_registration_rejected() -> None:
    with pytest.raises(McpError, match="重复注册"):
        ToolRegistry([NmapTool(), NmapTool()])


def test_manifests_listing_contains_hash() -> None:
    registry = ToolRegistry([NmapTool(raw_dir="/tmp/x")])
    manifests = registry.manifests()
    assert [m.name for m in manifests] == ["nmap"]
    assert manifests[0].hash and len(manifests[0].hash) == 64


# ---------- dispatch schema 校验（E_SCHEMA_TOOL，不重试不抛错） ----------


def test_dispatch_unknown_tool_rejected_as_receipt() -> None:
    registry = ToolRegistry([NmapTool(raw_dir="/tmp/x")])
    out = registry.dispatch("sqlmap", {"url": " http://x"})
    assert out.exit_code is None
    assert "E_SCHEMA_TOOL" in out.summary
    assert "未知工具" in out.summary


def test_dispatch_extra_and_missing_params_rejected(tmp_path: Path) -> None:
    registry = ToolRegistry([NmapTool(raw_dir=tmp_path / "raw")])
    extra = registry.dispatch("nmap", {"target": "10.0.0.5", "-oG": "x"})
    assert "未知参数" in extra.summary
    missing = registry.dispatch("nmap", {"ports": "80"})
    assert "缺必填参数" in missing.summary
    assert missing.exit_code is None


def test_dispatch_control_characters_rejected(tmp_path: Path) -> None:
    """§7 安全表：命令执行拒绝控制字符注入。"""
    registry = ToolRegistry([NmapTool(raw_dir=tmp_path / "raw")])
    out = registry.dispatch("nmap", {"target": "10.0.0.5\x00; rm -rf /"})
    assert out.exit_code is None
    assert "控制字符" in out.summary


def test_dispatch_int_param_type_checked(tmp_path: Path) -> None:
    registry = ToolRegistry([AgentRunTool()])
    bad = registry.dispatch("agent_run", {"mission": "m", "persona": "p", "step_budget": "十"})
    assert "需 int" in bad.summary
    assert registry.schema_failures.get("agent_run") == 1


# ---------- M5-30：nmap summary 给端口结论而不是启动 banner ----------


def test_nmap_summary_prefers_port_lines(tmp_path: Path) -> None:
    binary = _write_script(
        tmp_path,
        'echo "Starting Nmap 7.95 ( https://nmap.org ) at 2026-10-29 10:23 EDT"\n'
        'echo "22/tcp  open  ssh     OpenSSH 9.2"\n'
        'echo "80/tcp  open  http    nginx 1.24"\n'
        'echo "Nmap done: 1 IP address (1 host up) scanned in 8.10 seconds"',
    )
    out = NmapTool(binary=binary, raw_dir=tmp_path / "raw").run({"target": "10.0.0.5"})
    assert out.exit_code == 0
    assert "22/tcp  open  ssh" in out.summary
    assert "80/tcp  open  http" in out.summary
    assert "Starting Nmap" not in out.summary  # banner 不再是摘要


# ---------- M5-27：签名进包 / 失败回执带签名 ----------


def test_tool_signature_marks_required_params() -> None:
    manifest = ToolManifest(
        name="nmap", params={"target": "str", "ports": "str"}, required=("target",)
    )
    assert tool_signature(manifest) == "nmap(target*:str, ports:str)"


def test_schema_failure_receipt_carries_signature(tmp_path: Path) -> None:
    """猜错参数时回执自带合法签名：一次失败就能改对，不必再靠试错逼近。"""
    registry = ToolRegistry([NmapTool(raw_dir=tmp_path / "raw")])
    out = registry.dispatch("nmap", {"target": "10.0.0.5", "args": "-sV"})
    assert out.exit_code is None
    assert "合法签名 nmap(target*:str, ports:str)" in out.summary


def test_tool_catalog_lists_signatures_and_failure_count(tmp_path: Path) -> None:
    """目录与 manifest 同源；同一工具反复被拒时行内点名失败次数。"""
    registry = ToolRegistry([NmapTool(raw_dir=tmp_path / "raw")])
    assert registry.tool_catalog() == [
        "nmap(target*:str, ports:str) — nmap 端口与服务扫描（-sV 恒开）"
    ]
    registry.dispatch("nmap", {})  # 缺 target
    registry.dispatch("nmap", {"target": "x", "scripts": "http-title"})  # 未知参数
    line = registry.tool_catalog()[0]
    assert "已因参数不合失败 2 次" in line


# ---------- 禁递归按调用栈（§3.6 规则 2） ----------


class RecursiveWorker:
    """故意在 run() 里再次 dispatch 另一个 in-process 工具：必须被栈守卫拒掉。"""

    manifest = ToolManifest(name="worker_probe", kind="in-process", params={}, required=())

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        return self._registry.dispatch(
            "agent_run", {"mission": "套娃", "persona": "x", "step_budget": "3"}
        )


def test_in_process_recursion_rejected_by_call_stack() -> None:
    registry = ToolRegistry([AgentRunTool(), NmapTool(raw_dir="/tmp/x")])
    probes = RecursiveWorker(registry)
    registry._adapters["worker_probe"] = probes  # 测试注入探针适配器
    out = registry.dispatch("worker_probe", {})
    assert out.exit_code is None
    assert "禁递归" in out.summary


def test_in_process_top_level_call_not_blocked(tmp_path: Path) -> None:
    """栈深=0 的正常级内嵌调用应放行（回执明说 M1 占位，不假执行）。"""
    registry = ToolRegistry([AgentRunTool()])
    out = registry.dispatch("agent_run", {"mission": "扫", "persona": "recon", "step_budget": "5"})
    assert "M1 占位" in out.summary


# ---------- nmap 适配器（argv 列表 + raw 产物真实指针） ----------


def test_build_argv_is_plain_list_without_shell(tmp_path: Path) -> None:
    tool = NmapTool(raw_dir=tmp_path / "raw")
    argv = tool.build_argv("10.0.0.5", "80,443")
    assert argv == [
        str(tool._binary),
        "-Pn",
        "-sV",
        "-p",
        "80,443",
        "10.0.0.5",  # 测试可触内部常量
    ]


def test_nmap_run_writes_raw_receipt(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "echo '22/tcp open  ssh'")
    tool = NmapTool(binary=script, raw_dir=tmp_path / "raw", timeout_s=10.0)
    out = tool.run({"target": "10.0.0.5", "ports": "22"})
    assert out.exit_code == 0
    assert "22/tcp open  ssh" in out.summary
    assert os.path.exists(out.raw_ref), "raw_ref 必须指向真实落盘文件"
    assert "22/tcp open  ssh" in Path(out.raw_ref).read_text(encoding="utf-8")


def test_nmap_run_nonzero_exit_lands_receipt_not_raise(tmp_path: Path) -> None:
    """exit_code≠0 = E_TOOL_FAIL 照落回执（失败不等于目标不存在，不带倾向语义）。"""
    script = _write_script(tmp_path, "exit 7")
    tool = NmapTool(binary=script, raw_dir=tmp_path / "raw")
    out = tool.run({"target": "10.0.0.5"})
    assert out.exit_code == 7
    assert out.summary


def test_nmap_missing_binary_returns_receipt(tmp_path: Path) -> None:
    tool = NmapTool(binary=tmp_path / "no-such-nmap", raw_dir=tmp_path / "raw")
    out = tool.run({"target": "10.0.0.5"})
    assert out.exit_code is None
    assert "E_TOOL_FAIL" in out.summary
    assert out.raw_ref == ""
