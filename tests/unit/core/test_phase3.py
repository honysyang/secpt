"""Phase 3 测试：更多工具 + nmap 结构化 + 报告生成。"""

from __future__ import annotations

import shutil

import pytest

from secpt.core.graph.memory import MemoryGraphStore
from secpt.core.graph.worldmodel import WorldModel
from secpt.core.tools.registry import build_registry
from secpt.report import Finding, Report, ReportGenerator
from secpt.runtime import subprocess as sp
from secpt.tools.recon.more import (
    NslookupTool,
    PingTool,
    TracerouteTool,
    WhatWebTool,
)
from secpt.tools.recon.nmap_parser import (
    HostInfo,
    PortInfo,
    hosts_to_edges,
    hosts_to_entities,
    parse_nmap_xml,
)

# ---- Fixtures ----


@pytest.fixture
def worldmodel() -> WorldModel:
    return WorldModel(MemoryGraphStore())


@pytest.fixture
def full_whitelist(monkeypatch: pytest.MonkeyPatch):
    """放行所有工具。"""
    from secpt.config.models import ToolsConfig

    all_tools = [
        "echo",
        "nmap",
        "curl",
        "dig",
        "whatweb",
        "nikto",
        "nslookup",
        "traceroute",
        "whois",
        "ping",
    ]
    cfg = ToolsConfig(whitelist=all_tools)
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)
    return all_tools


# ---- 工具注册 ----


def test_build_registry_all_tools(full_whitelist: list[str]) -> None:
    reg = build_registry(tool_names=full_whitelist)
    names = reg.names()
    for tool in full_whitelist:
        assert tool in names, f"{tool} not registered"


# ---- nmap XML 解析 ----


def test_parse_nmap_xml_empty() -> None:
    hosts = parse_nmap_xml("<?xml version='1.0'?><nmaprun></nmaprun>")
    assert hosts == []


def test_parse_nmap_xml_single_host() -> None:
    xml = """<?xml version="1.0"?>
<nmaprun>
  <host>
    <address addr="192.168.1.1" addrtype="ipv4"/>
    <hostnames><hostname name="router.local"/></hostnames>
    <ports>
      <port portid="22" protocol="tcp">
        <state state="open"/>
        <service name="ssh" product="OpenSSH" version="8.9"/>
      </port>
      <port portid="80" protocol="tcp">
        <state state="open"/>
        <service name="http" product="nginx" version="1.18"/>
      </port>
      <port portid="443" protocol="tcp">
        <state state="closed"/>
      </port>
    </ports>
    <os><osmatch name="Linux 5.x"/></os>
  </host>
</nmaprun>"""
    hosts = parse_nmap_xml(xml)
    assert len(hosts) == 1
    h = hosts[0]
    assert h.ip == "192.168.1.1"
    assert h.hostname == "router.local"
    assert h.os == "Linux 5.x"
    assert len(h.ports) == 3
    open_ports = [p for p in h.ports if p.state == "open"]
    assert len(open_ports) == 2
    assert open_ports[0].port == 22
    assert open_ports[0].service == "ssh"
    assert "OpenSSH" in open_ports[0].version


def test_parse_nmap_xml_malformed() -> None:
    hosts = parse_nmap_xml("not valid xml at all")
    assert hosts == []


def test_hosts_to_entities() -> None:
    hosts = [
        HostInfo(
            ip="10.0.0.1",
            hostname="test.local",
            ports=[
                PortInfo(port=22, protocol="tcp", state="open", service="ssh"),
                PortInfo(port=80, protocol="tcp", state="open", service="http"),
                PortInfo(port=443, protocol="tcp", state="closed"),  # 不应包含
            ],
        )
    ]
    entities = hosts_to_entities(hosts)
    eids = [e["eid"] for e in entities]
    assert "host.10.0.0.1" in eids
    assert "port.10.0.0.1.22" in eids
    assert "port.10.0.0.1.80" in eids
    assert "port.10.0.0.1.443" not in eids  # closed port excluded


def test_hosts_to_edges() -> None:
    hosts = [
        HostInfo(
            ip="10.0.0.1",
            ports=[PortInfo(port=22, protocol="tcp", state="open")],
        )
    ]
    edges = hosts_to_edges(hosts)
    assert len(edges) == 1
    assert edges[0]["src"] == "host.10.0.0.1"
    assert edges[0]["dst"] == "port.10.0.0.1.22"
    assert edges[0]["etype"] == "has_port"


# ---- 报告生成 ----


def test_report_generator_create(worldmodel: WorldModel) -> None:
    _gen = ReportGenerator(worldmodel)
    report = Report(
        title="Test Report",
        target="10.0.0.1",
        summary="Test summary",
        findings=[
            Finding(title="Open SSH", severity="info", description="22/tcp open"),
            Finding(title="Open HTTP", severity="medium", description="80/tcp open nginx"),
        ],
        hosts=[{"etype": "host", "props": {"ip": "10.0.0.1", "hostname": "test", "os": "Linux"}}],
    )
    assert report.title == "Test Report"
    assert len(report.findings) == 2


def test_report_to_markdown(worldmodel: WorldModel) -> None:
    gen = ReportGenerator(worldmodel)
    report = Report(
        title="Pentest Report",
        target="192.168.1.1",
        summary="Found 2 open ports",
        findings=[
            Finding(title="SSH Open", severity="info", description="22/tcp", evidence=["ev.1"]),
        ],
        hosts=[{"props": {"ip": "192.168.1.1", "hostname": "router", "os": "Linux"}}],
    )
    md = gen.to_markdown(report)
    assert "# Pentest Report" in md
    assert "192.168.1.1" in md
    assert "SSH Open" in md
    assert "22/tcp" in md
    assert "ev.1" in md
    assert "router" in md


def test_report_to_text(worldmodel: WorldModel) -> None:
    gen = ReportGenerator(worldmodel)
    report = Report(
        title="Pentest",
        target="10.0.0.1",
        summary="Summary here",
        findings=[Finding(title="Port 80", severity="low", description="HTTP open")],
        hosts=[],
    )
    text = gen.to_text(report)
    assert "Pentest" in text
    assert "10.0.0.1" in text
    assert "Port 80" in text
    assert "=" * 60 in text


# ---- 真实工具冒烟（有工具则跑，无则 skip） ----


HAS_WHATWEB = shutil.which("whatweb") is not None
HAS_NIKTO = shutil.which("nikto") is not None
HAS_NSLOOKUP = shutil.which("nslookup") is not None
HAS_TRACEROUTE = shutil.which("traceroute") is not None
HAS_WHOIS = shutil.which("whois") is not None
HAS_PING = shutil.which("ping") is not None


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_WHATWEB, reason="whatweb not installed")
async def test_whatweb_tool(full_whitelist) -> None:
    tool = WhatWebTool()
    out = await tool.run("http://127.0.0.1:1/")
    assert "Error" not in out[:20] or "Connection refused" in out


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_NSLOOKUP, reason="nslookup not installed")
async def test_nslookup_tool(full_whitelist) -> None:
    tool = NslookupTool()
    out = await tool.run("localhost")
    assert "Error" not in out[:20]


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_PING, reason="ping not installed")
async def test_ping_tool(full_whitelist) -> None:
    tool = PingTool()
    out = await tool.run("-c 1 127.0.0.1")
    assert "Error" not in out[:20] or "unreachable" in out.lower()


@pytest.mark.asyncio
@pytest.mark.skipif(not HAS_TRACEROUTE, reason="traceroute not installed")
async def test_traceroute_tool(full_whitelist) -> None:
    tool = TracerouteTool()
    out = await tool.run("127.0.0.1")
    assert "Error" not in out[:20]
