"""M5 MCP 工具池验收：8 步门全路径、协议会话、降级边界。

测试策略：fake MCP server 用 [sys.executable, "-c", 本地脚本]（argv 列表，
无 shell）在子进程里模拟 stdio JSON-RPC 协议——不引第三方依赖，也真实
打穿了「spawn → initialize → tools/call → 收割」的会话生命周期。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pa import envs
from pa.gateway import execute
from pa.infra import evidence
from pa.infra.executor import ProcResult
from pa.models import AcceptanceItem, Budget, MissionSpec
from pa.store.view import StoreView
from pa.store.writer import StoreWriter
from pa.tools import REGISTRY
from pa.tools.base import ToolContext
from pa.tools.mcp import McpServerSpec, parse_manifest, register, spec_of

# 假 MCP server：stdin 逐行读 JSON-RPC，按规范应答；tool_err / jsonrpc_err /
# slow_scan 三个工具名分别模拟「工具级 error」「协议级 error」「挂死不回」。
_SERVER_SRC = """
import json, sys, time

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    m = msg.get("method")
    if m == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }), flush=True)
    elif m == "notifications/initialized":
        pass
    elif m == "tools/call":
        p = msg.get("params") or {}
        name = p.get("name", "")
        args = p.get("arguments") or {}
        if name == "slow_scan":
            time.sleep(60)
        if "jsonrpc_err" in name:
            resp = {"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32601, "message": "no such tool"}}
        elif name == "tool_err":
            resp = {"jsonrpc": "2.0", "id": msg["id"],
                    "result": {"content": [{"type": "text", "text": "boom"}],
                               "isError": True}}
        else:
            text = f"ran {name} {json.dumps(args, sort_keys=True)}"
            resp = {"jsonrpc": "2.0", "id": msg["id"],
                    "result": {"content": [{"type": "text", "text": text}],
                               "isError": False}}
        print(json.dumps(resp), flush=True)
"""


def _manifest(**extra: object) -> str:
    return json.dumps(
        [{"name": "recon", "cmd": [sys.executable, "-c", _SERVER_SRC], **extra}]
    )


@pytest.fixture
def mcp_recon(monkeypatch):
    """inline manifest + 真子进程假 server 注册进 REGISTRY（用完即删，
    不污染其它测试的 set(REGISTRY) 断言）。"""
    monkeypatch.setenv(
        envs.MCP_JSON,
        _manifest(tools=["port_scan", "slow_scan", "tool_err", "jsonrpc_err_tool"]),
    )
    assert register(REGISTRY) == 1
    yield REGISTRY["mcp_recon"]
    del REGISTRY["mcp_recon"]


def _write_mission(root: Path, scope: tuple[str, ...] = ("10.0.0.0/24:80",)) -> None:
    spec = MissionSpec(
        objective="评估暴露面",
        targets=["10.0.0.5:80"],
        acceptance=[
            AcceptanceItem(
                id="a1", item="端口清单", evidence="port.open", target="10.0.0.5"
            )
        ],
        budget=Budget(),
        allowed_scope=list(scope),
    )
    (root / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")


def _sw(root: Path) -> tuple:
    return StoreView(root).restore(), StoreWriter(root)


# ── 1) 注册与声明校验 ──


def test_env_unset_registers_nothing(monkeypatch):
    """配置缺席 = 能力缺席：env 未设置时静默 0 条、REGISTRY 不动。"""
    monkeypatch.delenv(envs.MCP_JSON, raising=False)
    d: dict = {}
    assert register(d) == 0 and d == {}
    assert envs.mcp_json() is None


def test_env_bad_file_path_returns_none(monkeypatch):
    monkeypatch.setenv(envs.MCP_JSON, "/nonexistent/secpt/mcp-x.json")
    assert envs.mcp_json() is None


def test_register_from_file_path(monkeypatch, tmp_path):
    """声明也支持文件路径（内联 JSON 或 path 两等价形态）。"""
    p = tmp_path / "mcp.json"
    p.write_text(_manifest(), encoding="utf-8")
    monkeypatch.setenv(envs.MCP_JSON, str(p))
    d: dict = {}
    assert register(d) == 1
    assert d["mcp_recon"].name == "mcp_recon"
    assert d["mcp_recon"].risk_tier == "med"  # 声明缺省即保守中间档
    assert d["mcp_recon"].requires_network is True


def test_manifest_defaults_and_required_selector():
    s = parse_manifest(json.dumps([{"name": "ok", "cmd": ["/bin/x"]}]))[0]
    assert s.risk_tier == "med" and s.side_effect == "read"
    assert s.requires_network is True and s.tools == []
    spec = spec_of(s)
    assert spec.schema["required"] == ["mcp_tool"]  # 选择器必填（gateway 锚点在顶层）


def test_manifest_bad_entries_skipped():
    """坏条目逐条跳过：部分可用优于全部拒绝（docstring 的设计理由）。"""
    raw = json.dumps(
        [
            {"name": "Upper Bad", "cmd": ["/x"]},  # 名字规则不过
            {"name": "ok1", "cmd": []},  # 空 cmd
            {"name": "ok2", "cmd": ["/x"], "risk_tier": "nuclear"},
            {"name": "ok3", "cmd": ["/x"], "side_effect": "water"},
            {"name": "ok4", "cmd": ["/x"], "requires_network": 1},
            {"name": "ok0", "cmd": ["/x"]},  # 合法对照
            "not-a-dict",
        ]
    )
    s = parse_manifest(raw)
    assert [x.name for x in s] == ["ok0"]


def test_manifest_broken_json_returns_empty():
    assert parse_manifest("{oops") == []
    assert parse_manifest('{"no": "list"}') == []


def test_servers_wrapper_form_roundtrip():
    """{"servers": [...]} 包装形态与裸数组等价。"""
    a = parse_manifest(_manifest())
    b = parse_manifest('{"servers": ' + _manifest() + "}")
    assert b == a


# ── 2) 网关 8 步门全路径（MCP 是普通 REGISTRY 条目，不存在旁路） ──


def test_gateway_full_path_ends_as_evidence(tmp_mission, mcp_recon):
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    args = {"mcp_tool": "port_scan", "target": "10.0.0.5", "port": 80}
    out = execute(state, w, tool_name="mcp_recon", args=args, task_id="t1")
    assert out.ok is True and out.evidence_id
    content = evidence.get(tmp_mission, out.evidence_id)
    assert content["tool"] == "mcp_recon" and content["exit"] == 0
    # server 返回的 text 走证据 stdout；顶层 target/port 被原样透传
    assert content["stdout"].startswith("ran port_scan")
    assert '"target": "10.0.0.5"' in content["stdout"]
    assert '"port": 80' in content["stdout"]


def test_gateway_out_of_scope_zero_execution(tmp_mission, mcp_recon):
    """范围门先于执行：越界目标 fn 从未 spawn，零证据只留 blocked。"""
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    out = execute(
        state,
        w,
        tool_name="mcp_recon",
        args={"mcp_tool": "port_scan", "target": "192.168.1.1", "port": 80},
        task_id="t1",
    )
    assert out.ok is False and out.blocked_reason == "out_of_scope"
    assert not (tmp_mission / "evidence").exists()


def test_gateway_timeout_key_fed_to_ctx_not_server(tmp_mission, mcp_recon):
    """timeout 是 secpt 控制键：进 ctx（会话 deadline），不透传给工具。"""
    _write_mission(tmp_mission)
    state, w = _sw(tmp_mission)
    out = execute(
        state,
        w,
        tool_name="mcp_recon",
        args={"mcp_tool": "port_scan", "target": "10.0.0.5", "port": 80, "timeout": 30},
        task_id="t1",
    )
    assert out.ok is True
    content = evidence.get(tmp_mission, out.evidence_id)
    assert '"timeout"' not in content["stdout"]  # server 没收到控制键


# ── 3) 会话层直调：成功 / 透传 / 显式 arguments / 各类降级 ──


def _ctx(tmp: Path, timeout: float = 60.0) -> ToolContext:
    return ToolContext(workspace=tmp, timeout=timeout)


def test_fn_explicit_arguments_win(tmp_path, mcp_recon):
    out: ProcResult = mcp_recon.fn(
        {"mcp_tool": "port_scan", "arguments": {"only_this": 1}}, _ctx(tmp_path)
    )
    assert out.exit_code == 0
    assert '"only_this": 1' in out.stdout.decode()
    assert "mcp_tool" not in out.stdout.decode()  # 选择器不进业务参数


def test_fn_jsonrpc_error_degrades(tmp_path, mcp_recon):
    out: ProcResult = mcp_recon.fn({"mcp_tool": "jsonrpc_err_tool"}, _ctx(tmp_path))
    assert out.exit_code == 1
    assert b"no such tool" in out.stderr


def test_fn_tool_level_iserror(tmp_path, mcp_recon):
    out: ProcResult = mcp_recon.fn({"mcp_tool": "tool_err"}, _ctx(tmp_path))
    assert out.exit_code == 1
    assert b"isError" in out.stdout  # 工具级错误内容保留进证据


def test_fn_missing_selector(tmp_path, mcp_recon):
    out: ProcResult = mcp_recon.fn({"target": "x"}, _ctx(tmp_path))
    assert out.exit_code == 1 and b"missing mcp_tool" in out.stderr


def test_fn_timeout_kills_server_group(tmp_path, mcp_recon):
    """挂死的 server 必须 killpg 收割而不是等 ctx 到期命数会话。"""
    out: ProcResult = mcp_recon.fn(
        {"mcp_tool": "slow_scan", "target": "10.0.0.5"}, _ctx(tmp_path, timeout=1.0)
    )
    assert out.exit_code == 124 and out.timed_out is True
    assert b"timed out" in out.stderr


def test_fn_crash_degrades_126(tmp_path):
    server = McpServerSpec(
        name="dead", cmd=[sys.executable, "-c", "import os; os._exit(7)"]
    )
    spec = spec_of(server)
    out: ProcResult = spec.fn({"mcp_tool": "x"}, _ctx(tmp_path))
    assert out.exit_code == 126 and b"mcp session failed" in out.stderr


def test_fn_cmd_not_spawnable_degrades(tmp_path):
    server = McpServerSpec(name="ghost", cmd=["/nonexistent/secpt/mcp-server"])
    spec = spec_of(server)
    out: ProcResult = spec.fn({"mcp_tool": "x"}, _ctx(tmp_path))
    assert out.exit_code == 126 and b"mcp session failed" in out.stderr
