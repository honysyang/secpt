"""recon_parse 插件：把扫描器输出确定性解析成资产/端点清单。

动机（通用）：nmap/ffuf/gobuster 的原始输出几千行，模型读原文既费 token 又易漏；
解析成 ws/assets.json（recon 角色契约：host/port/service/tech 数组）+ ws/endpoints.json，
后续 exploit/verify 直接消费，也供 intel 资产图摄取。

支持：
- nmap XML（-oX）：开放端口 + 服务/版本
- ffuf JSON（-o f.json）：URL/状态/长度/词数 → 端点
- gobuster/dirsearch 文本：`http://h/p (Status: 200)` 或 `/p (Status: 200)` → 端点
- format=auto 按内容嗅探；out/merge 可配。

红线：纯本地文件解析，不发网络请求、不执行外部命令。
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = {
    "type": "object",
    "description": (
        "解析扫描器输出为 ws/assets.json（host/port/service/tech）与 ws/endpoints.json。"
        "支持 nmap XML、ffuf JSON、gobuster/dirsearch 文本；自动嗅探格式。"
    ),
    "properties": {
        "path": {"type": "string", "description": "输入文件路径（相对路径基于当前工作区）"},
        "format": {
            "type": "string",
            "enum": ["auto", "nmap_xml", "ffuf_json", "text"],
            "description": "默认 auto",
        },
        "host": {"type": "string", "description": "text 模式下相对路径所需的 host（可选）"},
        "out": {"type": "string", "description": "资产输出路径（默认 assets.json）"},
        "merge": {"type": "boolean", "description": "与已有 assets.json 合并（默认 true）"},
    },
    "required": ["path"],
}


def _ws() -> Path:
    try:
        from secpt.runtime.workspace_ctx import current_work_dir

        d = current_work_dir()
        if d:
            return Path(d)
    except Exception:
        pass
    return Path(os.getcwd())


def _resolve(p: str, ws: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ws / path


def _sniff(text: str, suffix: str) -> str:
    if "<nmaprun" in text[:2000] or suffix == ".xml":
        return "nmap_xml"
    if suffix == ".json" or text.lstrip().startswith("{"):
        return "ffuf_json"
    return "text"


def _parse_nmap(text: str) -> tuple[list[dict], list[dict]]:
    assets: list[dict] = []
    endpoints: list[dict] = []
    root = ET.fromstring(text)
    for host in root.findall("host"):
        addr = ""
        for a in host.findall("address"):
            if a.get("addrtype") in ("ipv4", "ipv6"):
                addr = a.get("addr") or ""
                break
        for port in host.findall("./ports/port"):
            state = port.find("state")
            if state is None or state.get("state") != "open":
                continue
            svc = port.find("service")
            tech = " ".join(
                x
                for x in [
                    svc.get("product") if svc is not None else "",
                    svc.get("version") if svc is not None else "",
                ]
                if x
            )
            assets.append(
                {
                    "host": addr,
                    "port": int(port.get("portid") or 0),
                    "service": (svc.get("name") if svc is not None else "") or "",
                    "tech": tech,
                }
            )
            endpoints.append(
                {"host": addr, "port": int(port.get("portid") or 0), "path": "/", "status": 0}
            )
    return assets, endpoints


def _parse_ffuf(text: str) -> tuple[list[dict], list[dict]]:
    data = json.loads(text)
    rows = data.get("results") if isinstance(data, dict) else data
    assets: list[dict] = []
    endpoints: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for r in rows or []:
        url = str((r or {}).get("url") or "")
        if not url:
            continue
        u = urlsplit(url)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        if (host, port) not in seen:
            seen.add((host, port))
            assets.append({"host": host, "port": port, "service": u.scheme, "tech": "ffuf"})
        endpoints.append(
            {
                "url": url[:500],
                "host": host,
                "port": port,
                "path": (u.path or "/")[:500],
                "status": int(r.get("status") or 0),
                "length": int(r.get("length") or 0),
                "words": int(r.get("words") or 0),
            }
        )
    return assets, endpoints


_STATUS_RE = re.compile(r"(https?://\S+|/\S+?)\s+\(Status:\s*(\d{3})\)", re.I)


def _parse_text(text: str, host: str) -> tuple[list[dict], list[dict]]:
    endpoints: list[dict] = []
    for line in text.splitlines():
        m = _STATUS_RE.search(line)
        if not m:
            continue
        target, status = m.group(1), int(m.group(2))
        if target.startswith("http"):
            u = urlsplit(target)
            h, port, path = (
                u.hostname or host,
                u.port or (443 if u.scheme == "https" else 80),
                u.path,
            )
        else:
            h, port, path = host, 0, target
        endpoints.append(
            {"host": h, "port": port, "path": path[:500], "status": status, "url": target[:500]}
        )
    assets: list[dict] = []
    if endpoints and (host or endpoints[0].get("host")):
        seen: set[tuple[str, int]] = set()
        for e in endpoints:
            key = (str(e.get("host") or ""), int(e.get("port") or 0))
            if key[0] and key not in seen:
                seen.add(key)
                assets.append({"host": key[0], "port": key[1], "service": "http", "tech": "web"})
    return assets, endpoints


def _merge(path: Path, new_rows: list[dict], key_fields: tuple[str, ...]) -> int:
    old: list[dict] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                old = [x for x in data if isinstance(x, dict)]
        except Exception:
            old = []
    seen = {tuple(str(r.get(k, "")) for k in key_fields) for r in old}
    added = 0
    for r in new_rows:
        key = tuple(str(r.get(k, "")) for k in key_fields)
        if key in seen:
            continue
        seen.add(key)
        old.append(r)
        added += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    return added


def recon_parse(args: dict) -> str:
    args = args or {}
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "Error: path 必填"
    ws = _ws()
    src = _resolve(raw_path, ws)
    if not src.is_file():
        return f"Error: 文件不存在: {src}"
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error: 读取失败: {e}"
    fmt = str(args.get("format") or "auto")
    if fmt == "auto":
        fmt = _sniff(text, src.suffix.lower())
    host = str(args.get("host") or "")
    try:
        if fmt == "nmap_xml":
            assets, endpoints = _parse_nmap(text)
        elif fmt == "ffuf_json":
            assets, endpoints = _parse_ffuf(text)
        else:
            assets, endpoints = _parse_text(text, host)
    except Exception as e:
        return f"Error: 解析失败（format={fmt}）: {e}"
    merge = args.get("merge")
    merge = True if merge is None else bool(merge)
    out = _resolve(str(args.get("out") or "assets.json"), ws)
    ep_out = out.parent / "endpoints.json"
    if merge:
        added = _merge(out, assets, ("host", "port", "service", "tech"))
        ep_added = _merge(ep_out, endpoints, ("host", "port", "path", "status"))
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(assets, ensure_ascii=False, indent=1), encoding="utf-8")
        ep_out.write_text(json.dumps(endpoints, ensure_ascii=False, indent=1), encoding="utf-8")
        added, ep_added = len(assets), len(endpoints)
    return json.dumps(
        {
            "ok": True,
            "format": fmt,
            "assets_added": added,
            "endpoints_added": ep_added,
            "assets_file": str(out),
            "endpoints_file": str(ep_out),
            "sample_assets": assets[:5],
            "sample_endpoints": endpoints[:5],
        },
        ensure_ascii=False,
    )


def register() -> dict:
    return {"recon_parse": (SCHEMA, recon_parse)}
