"""nmap 输出解析：XML → 结构化实体。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class PortInfo:
    """一个开放端口的信息。"""

    port: int
    protocol: str
    state: str
    service: str = ""
    version: str = ""


@dataclass
class HostInfo:
    """一台主机的扫描结果。"""

    ip: str
    hostname: str = ""
    ports: list[PortInfo] = field(default_factory=list)
    os: str = ""


def parse_nmap_xml(xml_text: str) -> list[HostInfo]:
    """解析 nmap XML 输出为结构化 HostInfo 列表。"""
    hosts: list[HostInfo] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return hosts

    for host_elem in root.findall(".//host"):
        # 地址
        ip = ""
        for addr in host_elem.findall("address"):
            if addr.get("addrtype") == "ipv4":
                ip = addr.get("addr", "")

        # 主机名
        hostname = ""
        for hostnames in host_elem.findall("hostnames"):
            for hn in hostnames.findall("hostname"):
                hostname = hn.get("name", "")
                break

        # 端口
        ports: list[PortInfo] = []
        for ports_elem in host_elem.findall("ports"):
            for port_elem in ports_elem.findall("port"):
                port_id = int(port_elem.get("portid", "0"))
                protocol = port_elem.get("protocol", "tcp")
                state_elem = port_elem.find("state")
                state = state_elem.get("state", "unknown") if state_elem is not None else "unknown"

                service = ""
                version = ""
                service_elem = port_elem.find("service")
                if service_elem is not None:
                    service = service_elem.get("name", "")
                    product = service_elem.get("product", "")
                    version_str = service_elem.get("version", "")
                    if product:
                        version = product
                        if version_str:
                            version += f" {version_str}"

                ports.append(
                    PortInfo(
                        port=port_id,
                        protocol=protocol,
                        state=state,
                        service=service,
                        version=version,
                    )
                )

        # OS
        os_name = ""
        for os_elem in host_elem.findall("os"):
            for match in os_elem.findall("osmatch"):
                os_name = match.get("name", "")
                break

        hosts.append(HostInfo(ip=ip, hostname=hostname, ports=ports, os=os_name))

    return hosts


def hosts_to_entities(hosts: list[HostInfo]) -> list[dict]:
    """把 HostInfo 转为 WorldModel 实体列表。"""
    entities: list[dict] = []
    for h in hosts:
        entities.append(
            {
                "eid": f"host.{h.ip}",
                "etype": "host",
                "props": {"ip": h.ip, "hostname": h.hostname, "os": h.os},
            }
        )
        for p in h.ports:
            if p.state != "open":
                continue
            entities.append(
                {
                    "eid": f"port.{h.ip}.{p.port}",
                    "etype": "port",
                    "props": {
                        "port": p.port,
                        "protocol": p.protocol,
                        "service": p.service,
                        "version": p.version,
                    },
                }
            )
    return entities


def hosts_to_edges(hosts: list[HostInfo]) -> list[dict]:
    """把 HostInfo 转为 WorldModel 边列表。"""
    edges: list[dict] = []
    for h in hosts:
        for p in h.ports:
            if p.state != "open":
                continue
            edges.append(
                {
                    "src": f"host.{h.ip}",
                    "dst": f"port.{h.ip}.{p.port}",
                    "etype": "has_port",
                }
            )
    return edges


__all__ = ["HostInfo", "PortInfo", "hosts_to_edges", "hosts_to_entities", "parse_nmap_xml"]
