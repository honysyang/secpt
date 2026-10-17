"""web_search 插件：联网检索（产品指纹 → CVE/文档/绕过技巧）。

服务型靶场的核心打法链路是"指纹识别 → 已知漏洞检索 → 利用"——此前模型全凭
参数记忆，这是 c 系列（AI 平台/图数据库/远端登录）全灭的直接原因。本插件提供
检索工具，后端按 env 自动选择（三选一，都未配 → 明确配置指引的 Error）：

- ``TAVILY_API_KEY``    → Tavily Search API（POST api.tavily.com/search）
- ``BRAVE_API_KEY``     → Brave Search API（GET api.search.brave.com）
- ``SECPT_SEARCH_URL``  → 自建 SearXNG（GET {url}/search?format=json）

治理说明：出站固定到搜索基础设施（与 LLM 端点同类，不是靶场目标），不声明
x-secpt-governance（scope 管的是"打谁"，不是"查资料用的搜索引擎"）；检索内容
的纪律由提示词承担（只查攻防资料，不外带任何目标数据）。

阻塞式 urllib 一律放 asyncio.to_thread（不冻事件循环）；任何失败返回 Error 文本。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request

OUT_CAP = 5000
_TIMEOUT_S = 15
_MAX_RESULTS = 8

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "检索词。`产品名+版本+漏洞类型` 效果最好（如 '泛微 e-cology SQL注入'、"
                "'ComfyUI manager RCE'、'LiteLLM CVE'）；查绕过技巧加 payload 关键词"
            ),
        },
        "max_results": {
            "type": "integer",
            "description": f"返回条数（缺省 5，上限 {_MAX_RESULTS}）",
        },
    },
    "required": ["query"],
}


def _backend() -> str:
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    if os.environ.get("BRAVE_API_KEY"):
        return "brave"
    if os.environ.get("SECPT_SEARCH_URL"):
        return "searxng"
    return ""


def _clip(s: str, n: int = 300) -> str:
    return " ".join(str(s or "").split())[:n]


def _tavily(query: str, n: int, key: str) -> list[dict]:
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({"api_key": key, "query": query, "max_results": n}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [
        {
            "title": _clip(x.get("title"), 150),
            "url": x.get("url", ""),
            "snippet": _clip(x.get("content")),
        }
        for x in (data.get("results") or [])[:n]
    ]


def _brave(query: str, n: int, key: str) -> list[dict]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": n}
    )
    req = urllib.request.Request(
        url, headers={"X-Subscription-Token": key, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    items = ((data.get("web") or {}).get("results")) or []
    return [
        {
            "title": _clip(i.get("title"), 150),
            "url": i.get("url", ""),
            "snippet": _clip(i.get("description")),
        }
        for i in items[:n]
    ]


def _searxng(query: str, n: int, base: str) -> list[dict]:
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [
        {
            "title": _clip(x.get("title"), 150),
            "url": x.get("url", ""),
            "snippet": _clip(x.get("content")),
        }
        for x in (data.get("results") or [])[:n]
    ]


def register() -> dict:
    async def web_search(args: dict) -> str:
        q = " ".join(str((args or {}).get("query") or "").split())
        if not q:
            return "Error: query 必填"
        try:
            n = max(1, min(_MAX_RESULTS, int((args or {}).get("max_results") or 5)))
        except (TypeError, ValueError):
            n = 5
        be = _backend()
        if not be:
            return (
                "Error: 未配置搜索后端（三选一）：export TAVILY_API_KEY=... 或 "
                "export BRAVE_API_KEY=... 或 export SECPT_SEARCH_URL=https://searx.example"
                "（自建 SearXNG，需在 settings.yml 打开 json 格式）"
            )

        def _do() -> list[dict]:
            if be == "tavily":
                return _tavily(q, n, os.environ["TAVILY_API_KEY"])
            if be == "brave":
                return _brave(q, n, os.environ["BRAVE_API_KEY"])
            return _searxng(q, n, os.environ["SECPT_SEARCH_URL"])

        try:
            results = await asyncio.to_thread(_do)
        except urllib.error.HTTPError as e:
            return f"Error: 搜索后端 HTTP {e.code}（{be}）——检查 key 有效性/额度/计费"
        except Exception as e:
            return f"Error: 搜索失败（{be}）: {e}"
        if not results:
            return "（无结果——换更通用的关键词，如去掉版本号）"
        lines = [
            f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        ]
        return "\n".join(lines)[:OUT_CAP]

    return {"web_search": (SEARCH_SCHEMA, web_search)}
