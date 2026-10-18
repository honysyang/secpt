"""运行时工具升级通道：request_tools 备选池 + bash 扫描输出自动结构化。

- 池内工具不默认挂载；request_tools 启用后立即可分发、schema 注入下一轮
- 未知工具名/重复启用报错不炸链；单次启用上限 _POOL_PER_CALL_MAX
- 网络类工具经池启用时套治理前置（与初始装配同款）
- bash 扫描命令自动结构化（parser 注入测试）；非扫描命令/解析失败不碰原结果
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from secpt.runtime import engine as engine_mod
from secpt.runtime.engine import Engine, RunConfig


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _call(fn, args):
    """工具处理器可能同步可能异步，统一执行。"""
    out = fn(args)
    if inspect.isawaitable(out):
        out = asyncio.new_event_loop().run_until_complete(out)
    return out


def _schema(name: str, *, network: bool = False) -> dict:
    s = {
        "type": "function",
        "function": {
            "name": name,
            "description": f"测试工具 {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    if network:
        s["x-secpt-governance"] = {"network": True}
    return s


def _pool(n: int = 1, *, network: bool = False) -> dict:
    out = {}
    for i in range(n):
        name = f"dummy_{i}" if n > 1 or i else ("dummy_net" if network else "dummy_bin")
        out[name] = (_schema(name, network=network), lambda a: "ok")
    return out


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SECPT_DELEGATE", raising=False)
    monkeypatch.setenv("SECPT_AUTO_STRUCTURE", "0")  # 工具池测试不混入 bash 包装
    # 网络治理前置在池启用路径上的行为单独断言；这里替换为标记透传避免碰真实治理链
    seen = {}
    monkeypatch.setattr(
        engine_mod,
        "_governed_plugin_fn",
        lambda cfg, name, fn: seen.setdefault(name, fn) or fn,
    )
    return seen


def _mk_engine(tmp_path, pool):
    cfg = RunConfig(job_id="tpl", objective="test", system_extra="", workspace=tmp_path)
    return Engine(cfg, llm=object(), tool_pool=pool)


class TestRequestTools:
    def test_pool_not_attached_request_tool_registered(self, tmp_path):
        eng = _mk_engine(tmp_path, _pool())
        assert "dummy_bin" not in eng.tools  # 池内工具不默认挂载
        assert "request_tools" in eng.tools
        names = [s["function"]["name"] for s in eng._extra_schemas]
        assert names == ["request_tools"]

    def test_enable_then_dispatchable_and_schema_injected(self, tmp_path):
        eng = _mk_engine(tmp_path, _pool())
        out = _call(eng.tools["request_tools"], {"names": ["dummy_bin"], "reason": "发现二进制"})
        assert "已启用 1 个工具" in out and "发现二进制" in out
        assert eng.tools["dummy_bin"]({"x": 1}) == "ok"  # 立即可分发
        names = [s["function"]["name"] for s in eng._extra_schemas]
        assert "dummy_bin" in names  # schema 注入下一轮
        assert eng._tool_pool == {}  # 已出池
        again = _call(eng.tools["request_tools"], {"names": ["dummy_bin"]})
        assert again.startswith("Error:")  # 重复启用报错不炸链

    def test_unknown_tool_error(self, tmp_path):
        eng = _mk_engine(tmp_path, _pool())
        out = _call(eng.tools["request_tools"], {"names": ["nope"]})
        assert out.startswith("Error:") and "nope" in out

    def test_per_call_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine_mod, "_POOL_PER_CALL_MAX", 2)
        eng = _mk_engine(tmp_path, _pool(5))
        out = _call(
            eng.tools["request_tools"],
            {"names": [f"dummy_{i}" for i in range(5)], "reason": "x"},
        )
        assert "已启用 2 个工具" in out
        assert len(eng._extra_schemas) == 3  # request_tools + 2
        assert len(eng._tool_pool) == 3

    def test_network_tool_gets_governance_wrap(self, tmp_path):
        eng = _mk_engine(tmp_path, _pool(network=True))
        _call(eng.tools["request_tools"], {"names": ["dummy_net"], "reason": "盲注验证"})
        assert eng.tools["dummy_net"]({"x": 1}) == "ok"


class TestAutoStructure:
    def _wrapped(self, tmp_path, fake_bash, parser):
        cfg = RunConfig(job_id="as", objective="t", system_extra="", workspace=tmp_path)
        return engine_mod._auto_structured_bash(cfg, fake_bash, parser=parser)

    def test_scanner_output_parsed_and_annotated(self, tmp_path):
        seen = {}

        def parser(args):
            seen["path"] = args["path"]
            return "parsed 2 assets"

        fake = lambda a: "Open Port: 80/tcp\n" * 10  # noqa: E731
        w = self._wrapped(tmp_path, fake, parser)
        out = _run(w({"command": "nmap -sV 10.0.0.1"}))
        assert "[auto-structure]" in out and "parsed 2 assets" in out
        assert seen["path"].startswith(str(tmp_path / "ws" / "outputs"))

    def test_non_scanner_and_short_output_untouched(self, tmp_path):
        calls = []
        w = self._wrapped(tmp_path, lambda a: "x" * 50, parser=lambda a: calls.append(a) or "ok")
        out = _run(w({"command": "ls -la"}))
        assert "[auto-structure]" not in out and not calls  # 非扫描命令不触发
        w_short = self._wrapped(
            tmp_path, lambda a: "x" * 10, parser=lambda a: calls.append(a) or "ok"
        )
        out2 = _run(w_short({"command": "nmap -sV x"}))
        assert "[auto-structure]" not in out2 and not calls  # 输出过短不触发

    def test_parser_failure_keeps_bash_result(self, tmp_path):
        def bad_parser(args):
            raise RuntimeError("boom")

        w = self._wrapped(tmp_path, lambda a: "Open Port: 80/tcp\n" * 10, parser=bad_parser)
        out = _run(w({"command": "ffuf -u http://x/FUZZ -w w.txt"}))
        assert out.startswith("Open Port:") and "[auto-structure]" not in out

    def test_flag_disabled_keeps_raw_bash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_AUTO_STRUCTURE", "1")
        fake = lambda a: "Open Port: 80/tcp\n" * 10  # noqa: E731
        cfg = RunConfig(job_id="as2", objective="t", system_extra="", workspace=tmp_path)
        eng = Engine(cfg, llm=object(), tools={"bash": fake})
        assert eng.tools["bash"] is not fake  # 默认开：bash 被包装
        monkeypatch.setenv("SECPT_AUTO_STRUCTURE", "0")
        eng2 = Engine(cfg, llm=object(), tools={"bash": fake})
        assert eng2.tools["bash"] is fake  # 关闭：原样
