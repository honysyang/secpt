"""browser 插件单测：注册契约 / 参数路由 / 路径边界 / 缺依赖报错。

真实浏览器不在常规 CI 跑（playwright 属可选依赖）——通过 _call_op 替换点做
行为级断言（无浏览器也可测全链路），有 playwright 时另跑一个 data:URL 真实
冒烟（跳过制，不触网）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from secpt.tools.plugin_loader import OUT_CAP

PLUGINS_DIR = Path(__file__).resolve().parents[3] / "tools" / "plugins"

try:
    import playwright  # noqa: F401

    _HAS_PW = True
except ImportError:
    _HAS_PW = False


def _load():
    """按 plugin_loader 同款方式加载（模块级状态干净：不污染 sys.modules 共享实例）。"""
    spec = importlib.util.spec_from_file_location(
        "secpt_plugin_browser", PLUGINS_DIR / "browser.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_register_contract():
    tools = _load().register()
    assert set(tools) == {
        "browser_navigate",
        "browser_click",
        "browser_fill",
        "browser_screenshot",
        "browser_get_content",
    }
    for name, (schema, fn) in tools.items():
        assert schema["type"] == "object", name
        assert set(schema.get("required") or []) <= set(schema["properties"]), name
        assert callable(fn), name


def test_network_tools_declare_governance():
    """触网工具必须声明 x-secpt-governance.network（engine 层治理前置的接入凭据）。"""
    tools = _load().register()
    for name in ("browser_navigate", "browser_click", "browser_fill"):
        assert tools[name][0]["x-secpt-governance"] == {"network": True}, name
    for name in ("browser_screenshot", "browser_get_content"):
        assert "x-secpt-governance" not in tools[name][0], name  # 不新触网：无需目标治理


def test_loader_accepts_plugin(monkeypatch):
    """plugin_loader 的 Python 插件校验必须放行（schema/命名/fn 契约）。"""
    from secpt.tools.plugin_loader import load_python_plugin

    tools = load_python_plugin(PLUGINS_DIR / "browser.py")
    assert "browser_navigate" in tools


def test_navigate_routes_and_serializes(monkeypatch):
    b = _load()
    seen = []

    async def _fake_call(sync_fn, *args):
        seen.append((sync_fn.__name__, args))
        return json.dumps({"url": args[0]})

    monkeypatch.setattr(b, "_call_op", _fake_call)
    out = asyncio.run(b.register()["browser_navigate"][1]({"url": "http://t/"}))
    assert json.loads(out)["url"] == "http://t/"
    assert seen == [("_op_navigate", ("http://t/",))]


def test_click_and_fill_arg_routing(monkeypatch):
    b = _load()
    seen = []

    async def _fake_call(sync_fn, *args):
        seen.append((sync_fn.__name__, args))
        return "OK"

    monkeypatch.setattr(b, "_call_op", _fake_call)
    tools = b.register()
    assert asyncio.run(tools["browser_click"][1]({"selector": "#go"})) == "OK"
    assert asyncio.run(tools["browser_fill"][1]({"selector": "#user", "value": "admin"})) == "OK"
    assert seen == [("_op_click", ("#go",)), ("_op_fill", ("#user", "admin"))]
    # 必填参数缺失 → 明确 Error，不走调度
    assert asyncio.run(tools["browser_click"][1]({})).startswith("Error:")


def test_op_error_wrapped(monkeypatch):
    b = _load()

    async def _boom(sync_fn, *args):
        raise RuntimeError("页面崩溃")

    monkeypatch.setattr(b, "_call_op", _boom)
    out = asyncio.run(b.register()["browser_navigate"][1]({"url": "http://t/"}))
    assert out.startswith("Error:") and "页面崩溃" in out


class _FakePage:
    def __init__(self):
        self.shots = []

    def screenshot(self, path, full_page=True):
        self.shots.append((path, full_page))

    def is_closed(self):
        return False


def test_screenshot_boundary_and_default(monkeypatch, tmp_path):
    b = _load()
    monkeypatch.setenv("SECPT_WORK_DIR", str(tmp_path))
    page = _FakePage()
    monkeypatch.setattr(b._Session, "ensure_page", lambda self: page)
    s = b._Session()
    assert b._op_screenshot(s, "shot.png").startswith("OK:")
    assert Path(page.shots[0][0]).resolve() == (tmp_path / "shot.png").resolve()
    assert page.shots[0][1] is True  # full_page
    # 与 write_file 同边界：工作目录外一律拒绝
    out = b._op_screenshot(s, "../../etc/x.png")
    assert out.startswith("Error:") and "只能写到" in out


def test_missing_playwright_friendly_error(monkeypatch, tmp_path):
    """未装 playwright：真实调度路径（专用线程）返回含安装指引的 Error。"""
    b = _load()
    monkeypatch.setenv("SECPT_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(b, "_SESSION", None)  # 干净会话（避免复用其他测试的实例）

    def _no_pw():
        raise ImportError("No module named 'playwright'")

    monkeypatch.setattr(b, "_load_sync_playwright", _no_pw)
    out = asyncio.run(b.register()["browser_navigate"][1]({"url": "http://10.1.2.3/"}))
    assert out.startswith("Error:")
    assert "playwright" in out and "install" in out  # 指引在错误里，不静默


@pytest.mark.skipif(not _HAS_PW, reason="playwright 未安装（可选依赖）")
def test_real_browser_smoke(monkeypatch, tmp_path):
    """真实冒烟：data:URL（不触网）→ 标题/正文取回 + 截图落盘。"""
    b = _load()
    monkeypatch.setenv("SECPT_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("SECPT_BROWSER_NO_SANDBOX", "1")
    monkeypatch.setattr(b, "_SESSION", None)
    tools = b.register()
    out = asyncio.run(
        tools["browser_navigate"][1](
            {"url": "data:text/html,<title>T</title><body>hello-browser</body>"}
        )
    )
    data = json.loads(out)
    assert data["title"] == "T"
    content = json.loads(asyncio.run(tools["browser_get_content"][1]({})))
    assert "hello-browser" in content["text"]
    assert asyncio.run(tools["browser_screenshot"][1]({"path": "shot.png"})).startswith("OK:")
    assert (tmp_path / "shot.png").is_file()


class _FakeChromium:
    """chromium.launch 替身：记录调用，可配置"自带二进制启动失败"。"""

    def __init__(self, fail_bundled: bool = True):
        self.fail_bundled = fail_bundled
        self.calls = []

    def launch(self, *, headless=False, args=None, executable_path=None):
        self.calls.append({"headless": headless, "args": args, "executable_path": executable_path})
        if executable_path is None and self.fail_bundled:
            raise RuntimeError("Executable doesn't exist at $HOME/.cache/ms-playwright")
        return f"browser@{executable_path or 'bundled'}"


def _sess_with(chrom):
    """构造已注入 playwright 替身的 _Session（不经 executor，直测启动逻辑）。"""
    b = _load()
    s = b._Session()
    s.pw = type("_PW", (), {"chromium": chrom})()
    return s


class TestLaunchResilience:
    """无头浏览器不可用路径：系统 chromium 回退 / 两路失败可行动报错 / no-sandbox 透传。"""

    def test_falls_back_to_system_chromium(self, monkeypatch):
        chrom = _FakeChromium()
        s = _sess_with(chrom)
        monkeypatch.setattr("shutil.which", lambda c: f"/usr/bin/{c}" if c == "chromium" else None)
        assert s._launch_browser() == "browser@/usr/bin/chromium"
        assert chrom.calls[0]["executable_path"] is None  # 先试 playwright 自带
        assert chrom.calls[1]["executable_path"] == "/usr/bin/chromium"  # 失败后回退系统

    def test_unavailable_raises_actionable_error(self, monkeypatch):
        """两路都失败：RuntimeError 含安装指引（不静默、不半初始化）。"""
        s = _sess_with(_FakeChromium())
        monkeypatch.setattr("shutil.which", lambda c: None)
        with pytest.raises(RuntimeError, match="playwright install"):
            s._launch_browser()

    def test_no_sandbox_passthrough(self, monkeypatch):
        chrom = _FakeChromium(fail_bundled=False)
        s = _sess_with(chrom)
        monkeypatch.setenv("SECPT_BROWSER_NO_SANDBOX", "1")
        s._launch_browser()
        assert chrom.calls[0]["args"] == ["--no-sandbox"]
        assert chrom.calls[0]["headless"] is True  # 默认无头


class TestArgContract:
    """参数契约：未知参数拒绝 + 输出 OUT_CAP 兜底（engine 的 OUT_CAP 只覆盖 bash）。"""

    def test_unknown_args_rejected_before_dispatch(self, monkeypatch):
        b = _load()
        seen = []

        async def _spy(sync_fn, *args):
            seen.append(args)
            return "OK"

        monkeypatch.setattr(b, "_call_op", _spy)
        tools = b.register()
        cases = [
            ("browser_navigate", {"url": "http://t/", "evil": "1"}),
            ("browser_click", {"selector": "#x", "evil": "1"}),
            ("browser_fill", {"selector": "#x", "value": "v", "evil": "1"}),
            ("browser_screenshot", {"path": "a.png", "evil": "1"}),
            ("browser_get_content", {"evil": "1"}),
        ]
        for name, args in cases:
            out = asyncio.run(tools[name][1](args))
            assert out.startswith("Error: 未知参数") and "evil" in out, name
        assert seen == []  # 拒绝发生在调度前

    def test_output_capped_to_out_cap(self, monkeypatch):
        b = _load()

        async def _huge(sync_fn, *args):
            return "x" * (OUT_CAP + 500)

        monkeypatch.setattr(b, "_call_op", _huge)
        out = asyncio.run(b.register()["browser_get_content"][1]({}))
        assert out[:OUT_CAP] == "x" * OUT_CAP  # 正文截到 OUT_CAP
        assert "8500" in out and "截断" in out  # 保留原始长度提示
        assert len(out) < OUT_CAP + 120
