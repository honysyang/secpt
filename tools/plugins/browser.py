"""browser 插件：Playwright 驱动的真实浏览器（登录态 / JS 渲染 / DOM 交互）。

补 secpt 的即战力缺口：现代 Web 渗透（登录态、JS 渲染、DOM XSS）靠 curl
缺席半个战场。会话随工作区持久化（ws/browser-state.json）——登录态跨调用、
跨进程重启可续用（storage_state 在每次操作后回写）。

线程模型（与 intercept 修复同哲学：不得冻结事件循环）：
- Playwright sync API 要求固定单线程 → 专用单线程 executor 串行执行所有操作
- 异步包装只做 run_in_executor 调度，事件循环永不执行浏览器阻塞调用
- 各操作由 Playwright 自身超时界定（导航 30s / 操作 15s），不依赖外部 kill

治理：navigate/click/fill 声明 x-secpt-governance.network=true——engine 装配
时统一做 scope/ratelimit/intercept 前置（目标提取口径与 bash 一致）；声明键是 engine
私有协议，不随 schema 发往 LLM（engine 侧剥离）。

依赖（可选）：pip install playwright && playwright install chromium
缺失时工具返回明确 Error（含安装指引），不影响其余功能。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from secpt.tools.plugin_loader import OUT_CAP

_NAV_TIMEOUT_MS = 30_000
_OP_TIMEOUT_MS = 15_000
_TEXT_CAP = 6000  # 页面正文回传上限（整体仍受 _guarded 的 OUT_CAP=8000 兜底）

_GOV = {"network": True}  # x-secpt-governance 声明（engine 层治理前置）

# 系统 chromium 候选（自带二进制缺失时的回退探测顺序）
_SYSTEM_CHROMIUM = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")


def _bad_args(args: dict, allowed: set[str]) -> str | None:
    """未知参数拒绝（与 YAML 插件 make_plugin_fn 同语义）：schema 未声明一律拒。

    为什么在插件侧做而非依赖 LLM 的 schema 遵从：模型偶发拼错/多传参数，静默
    忽略会让它误以为参数已生效（如以为改了配置），显式报错才能自纠不烧步数。
    """
    unknown = set(args or {}) - allowed
    if unknown:
        return f"Error: 未知参数 {sorted(unknown)}，可用：{sorted(allowed)}"
    return None

NAVIGATE_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "目标 URL（含 http(s)://）"}},
    "required": ["url"],
    "x-secpt-governance": _GOV,
}
CLICK_SCHEMA = {
    "type": "object",
    "properties": {"selector": {"type": "string", "description": "CSS selector"}},
    "required": ["selector"],
    "x-secpt-governance": _GOV,
}
FILL_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": "CSS selector"},
        "value": {"type": "string", "description": "填入的值"},
    },
    "required": ["selector", "value"],
    "x-secpt-governance": _GOV,
}
SCREENSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对工作目录的保存路径，如 shot.png"}
    },
    "required": ["path"],
}
CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": "可选：只取该元素的文本，缺省 body"}
    },
}


def _load_sync_playwright():
    """seam：懒加载 playwright（单测可替换以模拟未安装）。"""
    from playwright.sync_api import sync_playwright

    return sync_playwright


def _work_dir() -> Path:
    return Path(os.environ.get("SECPT_WORK_DIR", ".")).resolve()


def _state_path() -> Path:
    return _work_dir() / "browser-state.json"


class _Session:
    """浏览器会话：专用单线程 executor + 惰性启动的 playwright 对象。

    所有 ensure_page/op 函数只允许在该 executor 的线程上执行（sync API 合约）。
    """

    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="secpt-browser")
        self.headless = os.environ.get("SECPT_BROWSER_HEADLESS", "1") != "0"
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None

    def _launch_browser(self):
        """启动 chromium：优先 playwright 自带二进制；缺失时回退系统 chromium；
        两路都失败 → 抛可行动的 RuntimeError（含安装指引），不静默不半初始化。

        为什么回退系统 chromium：宿主常见"pip 装了 playwright 但没跑
        playwright install"——自带二进制缺失而系统 chromium 可用，直接报错会把
        可用能力挡在门外；探测顺序见 _SYSTEM_CHROMIUM。
        """
        # 容器内以宿主 uid 运行且常无 user namespace：显式开启 no-sandbox 开关
        args = ["--no-sandbox"] if os.environ.get("SECPT_BROWSER_NO_SANDBOX") == "1" else []
        try:
            return self.pw.chromium.launch(headless=self.headless, args=args)
        except Exception as bundled_err:
            for exe in (shutil.which(c) for c in _SYSTEM_CHROMIUM):
                if not exe:
                    continue
                with contextlib.suppress(Exception):
                    return self.pw.chromium.launch(
                        headless=self.headless, args=args, executable_path=exe
                    )
            first = str(bundled_err).splitlines()[0][:160]
            raise RuntimeError(
                "无头浏览器不可用：playwright 自带 chromium 启动失败，且系统无可用 chromium"
                f"（{first}）。修复：pip install playwright && playwright install chromium，"
                "或安装系统 chromium（如 apt install chromium）后重试；"
                "容器内以非 root 运行需 SECPT_BROWSER_NO_SANDBOX=1"
            ) from bundled_err

    def ensure_page(self):
        """惰性创建 chromium context/page（仅限 executor 线程调用）。"""
        if self.page is not None and not self.page.is_closed():
            return self.page
        try:
            sp = _load_sync_playwright()
        except ImportError:
            raise RuntimeError(
                "playwright 未安装：pip install playwright && playwright install chromium"
            )
        self.pw = sp().start()
        self.browser = self._launch_browser()
        state = _state_path()
        self.context = self.browser.new_context(
            storage_state=str(state) if state.is_file() else None
        )
        self.context.set_default_timeout(_OP_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
        self.page = self.context.new_page()
        return self.page

    def persist(self) -> None:
        """登录态回写（best-effort：持久化失败不影响本次操作）。"""
        if self.context is None:
            return
        with contextlib.suppress(Exception):
            self.context.storage_state(path=str(_state_path()))


# ---------------------------------------------------------------------------
# 同步操作（模块级纯函数：测试可直接调用，绕过线程调度）
# ---------------------------------------------------------------------------


def _op_navigate(s: _Session, url: str) -> str:
    page = s.ensure_page()
    resp = page.goto(url, wait_until="domcontentloaded")
    s.persist()
    return json.dumps(
        {
            "url": page.url,
            "status": resp.status if resp else None,
            "title": (page.title() or "")[:200],
        },
        ensure_ascii=False,
    )


def _op_click(s: _Session, selector: str) -> str:
    page = s.ensure_page()
    page.click(selector)
    # 点击可能触发导航；非导航交互下立即返回（domcontentloaded 已满足）
    with contextlib.suppress(Exception):
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
    s.persist()
    return json.dumps({"url": page.url, "title": (page.title() or "")[:200]}, ensure_ascii=False)


def _op_fill(s: _Session, selector: str, value: str) -> str:
    page = s.ensure_page()
    page.fill(selector, value)
    s.persist()
    return f"OK: filled {selector!r}"


def _op_screenshot(s: _Session, path: str) -> str:
    page = s.ensure_page()
    wr = _work_dir()
    p = Path(path)
    if not p.is_absolute():
        p = wr / p
    try:
        pr = p.resolve()
        if not (str(pr) == str(wr) or str(pr).startswith(str(wr) + os.sep)):
            return f"Error: 只能写到 {wr}/ 下（你传的是 {path!r}）"
    except Exception:
        return f"Error: bad path {path!r}"
    p.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(p), full_page=True)
    return f"OK: screenshot -> {p.name}"


def _op_content(s: _Session, selector: str = "") -> str:
    page = s.ensure_page()
    s.persist()
    try:
        text = page.inner_text(selector or "body")
    except Exception as e:
        text = f"(无法取文本: {e})"
    html_len = 0
    with contextlib.suppress(Exception):
        html_len = len(page.content())
    return json.dumps(
        {
            "url": page.url,
            "title": (page.title() or "")[:200],
            "text": text[:_TEXT_CAP],
            "text_truncated": len(text) > _TEXT_CAP,
            "html_len": html_len,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 异步调度（专用单线程 executor：sync API 单线程合约 + 不阻塞事件循环）
# ---------------------------------------------------------------------------

_SESSION: _Session | None = None


def _get_session() -> _Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _Session()
    return _SESSION


async def _call_op(sync_fn, *args) -> str:
    """统一的会话单线程调度入口（单测替换点）。"""
    s = _get_session()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(s.executor, sync_fn, s, *args)


async def _guarded(sync_fn, *args) -> str:
    """统一出口：异常转 Error 串 + 输出长度对齐 OUT_CAP（与 plugin_loader/引擎一致）。

    为什么插件侧截断而非靠 engine：engine 的 OUT_CAP 只作用于 bash 工具，插件
    结果原样进 messages——超长页面正文没有兜底会撑爆上下文窗口。
    """
    try:
        out = await _call_op(sync_fn, *args)
    except Exception as e:
        return f"Error: {e}"
    if len(out) > OUT_CAP:
        out = out[:OUT_CAP] + (
            f"\n...(输出超长已截断：原始 {len(out)} 字符；细节可分次取或用 browser_screenshot)"
        )
    return out


def register() -> dict:
    async def browser_navigate(args: dict) -> str:
        if err := _bad_args(args, {"url"}):
            return err
        url = str(args.get("url") or "").strip()
        if not url:
            return "Error: url 必填"
        return await _guarded(_op_navigate, url)

    async def browser_click(args: dict) -> str:
        if err := _bad_args(args, {"selector"}):
            return err
        sel = str(args.get("selector") or "").strip()
        if not sel:
            return "Error: selector 必填"
        return await _guarded(_op_click, sel)

    async def browser_fill(args: dict) -> str:
        if err := _bad_args(args, {"selector", "value"}):
            return err
        sel = str(args.get("selector") or "").strip()
        if not sel:
            return "Error: selector 必填"
        return await _guarded(_op_fill, sel, str(args.get("value") or ""))

    async def browser_screenshot(args: dict) -> str:
        if err := _bad_args(args, {"path"}):
            return err
        path = str(args.get("path") or "").strip() or "screenshot.png"
        return await _guarded(_op_screenshot, path)

    async def browser_get_content(args: dict) -> str:
        if err := _bad_args(args, {"selector"}):
            return err
        return await _guarded(_op_content, str(args.get("selector") or ""))

    return {
        "browser_navigate": (NAVIGATE_SCHEMA, browser_navigate),
        "browser_click": (CLICK_SCHEMA, browser_click),
        "browser_fill": (FILL_SCHEMA, browser_fill),
        "browser_screenshot": (SCREENSHOT_SCHEMA, browser_screenshot),
        "browser_get_content": (CONTENT_SCHEMA, browser_get_content),
    }
