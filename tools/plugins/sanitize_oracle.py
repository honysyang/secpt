"""sanitize_oracle 插件：净化器/客户端盾牌的本地差分 oracle。

把 author_plus 攻坚时手搓的 /tmp/mxss-oracle 产品化：遇到"附件含 sanitize/
过滤代码 + 客户端 shield"类题目（intel/recall 提示或题面特征），agent 装配
本插件后可在本地无限快验证 payload，而不是远程盲测烧步数。

工具：
- sanitize_probe(attachments_dir, payload)  —— 用题目自带的 sanitize.js
  跑双重净化（与靶场服务端完全一致），返回净化输出
- browser_check(html_fragment)              —— Chromium 解析净化输出，
  检测"解析时可执行 markup"（事件处理器/javascript: 复活 = mXSS 命中）
- mxss_probe(attachments_dir, payload)      —— 两者合一的判定接口

依赖：node（跑 sanitize.js）+ chromium（browser_check）；npm install 首次
自动执行并缓存（var/oracle-cache/<sha1>/）。任一缺失 → 明确 Error，不静默。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

_ORACLE_ROOT = Path(os.environ.get("SECPT_VAR", "var")) / "oracle-cache"
_NPM_TIMEOUT_S = 300
_CHROME_TIMEOUT_S = 20

SANITIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "attachments_dir": {
            "type": "string",
            "description": "附件源码目录（含 sanitize.js 的那层，如 attachments/author_plus/blog）",
        },
        "payload": {"type": "string", "description": "待净化的 HTML payload"},
    },
    "required": ["attachments_dir", "payload"],
}

BROWSER_SCHEMA = {
    "type": "object",
    "properties": {
        "html_fragment": {"type": "string", "description": "净化后的 HTML 片段"},
    },
    "required": ["html_fragment"],
}

MXSS_SCHEMA = {
    "type": "object",
    "properties": {
        "attachments_dir": {"type": "string"},
        "payload": {"type": "string"},
    },
    "required": ["attachments_dir", "payload"],
}

_DOC = {
    "sanitize_probe": "用题目自带的 sanitize.js 本地跑双重净化（与靶场服务端一致），返回净化输出——遇到净化器类题目先本地验证再远程打",
    "browser_check": "Chromium 解析 HTML 片段，检测解析时复活的事件处理器/javascript:（mXSS 判定）",
    "mxss_probe": "一站式：双重净化 → 浏览器解析 → 判定 payload 是否在浏览器里变成可执行 markup",
}


def _which(bin_name: str) -> str | None:
    return shutil.which(bin_name)


def _find_sanitize_js(attachments_dir: str) -> Path | None:
    root = Path(attachments_dir)
    if not root.is_dir():
        return None
    for cand in root.rglob("sanitize.js"):
        return cand
    return None


def _prepare_workspace(src_dir: Path) -> Path:
    """把附件源码复制到缓存工作区（首次 npm install，之后复用）。

    缓存键 = 目录内容 sha1——附件不变就不重复装依赖（npm install 分钟级）。
    """
    h = hashlib.sha1()
    for f in sorted(src_dir.rglob("*")):
        if f.is_file() and "node_modules" not in f.parts:
            h.update(str(f.relative_to(src_dir)).encode())
            h.update(f.read_bytes()[:4096])
    ws = _ORACLE_ROOT / h.hexdigest()[:16]
    marker = ws / ".ready"
    if marker.is_file():
        return ws
    ws.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        dest = ws / item.name
        if item.is_dir() and item.name != "node_modules":
            shutil.copytree(item, dest, ignore=shutil.ignore_patterns("node_modules"))
        elif item.is_file():
            shutil.copy2(item, dest)
    if (ws / "package.json").is_file() and not (ws / "node_modules").is_dir():
        if not _which("npm"):
            raise RuntimeError("附件需要 npm install 但系统无 npm")
        r = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=_NPM_TIMEOUT_S,
        )
        if r.returncode != 0:
            raise RuntimeError(f"npm install 失败: {(r.stderr or '')[:300]}")
    marker.write_text("ok", encoding="utf-8")
    return ws


def _run_sanitize(ws: Path, payload: str) -> str:
    script = next(ws.rglob("sanitize.js"), None)
    if script is None:
        raise RuntimeError(f"工作区无 sanitize.js: {ws}")
    r = subprocess.run(
        ["node", str(script)],
        input=json.dumps({"content": payload}),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(script.parent),
    )
    if r.returncode != 0:
        raise RuntimeError(f"sanitize.js 执行失败: {(r.stderr or '')[:300]}")
    data = json.loads(r.stdout)
    if not data.get("success"):
        raise RuntimeError(f"sanitize 返回失败: {data.get('error')}")
    return str(data.get("content") or "")


def _browser_parse(fragment: str) -> dict:
    """Chromium 解析 → DOM 里可执行 markup 检测。"""
    if not (_which("chromium") or _which("chromium-browser") or _which("google-chrome")):
        raise RuntimeError("无 chromium（browser_check 需要）")
    chrome = _which("chromium") or _which("chromium-browser") or _which("google-chrome")
    page = _ORACLE_ROOT / "_probe_page.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        f"<!doctype html><html><body><div id='c'>{fragment}</div></body></html>",
        encoding="utf-8",
    )
    r = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=2000",
            "--dump-dom",
            f"file://{page}",
        ],
        capture_output=True,
        text=True,
        timeout=_CHROME_TIMEOUT_S,
        errors="replace",
    )
    dom = r.stdout
    import re

    handlers = re.findall(r"on\w+\s*=", dom)
    js_urls = re.findall(r"javascript:", dom, re.I)
    return {
        "has_event_handlers": bool(handlers),
        "handlers": sorted(set(handlers))[:10],
        "has_javascript_url": bool(js_urls),
        "dom_excerpt": dom[:600],
    }


def register() -> dict:
    async def sanitize_probe(args: dict) -> str:
        try:
            if not _which("node"):
                return "Error: 无 node（sanitize_probe 需要 Node.js）"
            src = Path(args["attachments_dir"]).resolve()
            if not src.is_dir():
                return f"Error: 附件目录不存在: {src}"
            ws = _prepare_workspace(src)
            clean = _run_sanitize(ws, str(args["payload"]))
            return json.dumps({"clean": clean}, ensure_ascii=False)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            return f"Error: {e}"

    async def browser_check(args: dict) -> str:
        try:
            return json.dumps(_browser_parse(str(args["html_fragment"])), ensure_ascii=False)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            return f"Error: {e}"

    async def mxss_probe(args: dict) -> str:
        try:
            if not _which("node"):
                return "Error: 无 node"
            src = Path(args["attachments_dir"]).resolve()
            ws = _prepare_workspace(src)
            clean = _run_sanitize(ws, str(args["payload"]))
            check = _browser_parse(clean)
            verdict = check["has_event_handlers"] or check["has_javascript_url"]
            return json.dumps(
                {
                    "mxss": verdict,
                    "clean": clean[:800],
                    **check,
                    "note": "mxss=true：净化输出在浏览器解析时复活为可执行 markup——"
                    "验证真实触发（起 bot/浏览器）后再 report_finding",
                },
                ensure_ascii=False,
            )
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            return f"Error: {e}"

    return {
        "sanitize_probe": (SANITIZE_SCHEMA, sanitize_probe),
        "browser_check": (BROWSER_SCHEMA, browser_check),
        "mxss_probe": (MXSS_SCHEMA, mxss_probe),
    }
