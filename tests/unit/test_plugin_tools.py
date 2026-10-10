"""插件工具系统测试：加载校验 / 模板注入防护 / 超时 / Engine 集成。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from secpt.tools.plugin_loader import (PluginError, engine_tools_for,
                                       load_yaml_plugin, make_plugin_fn)

from secpt.runtime.engine import Engine, RunConfig


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mk_plugin(tmp_path: Path, text: str, name: str = "p.yaml") -> Path:
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


VALID = """
name: getshell
description: 测试插件
schema:
  type: object
  properties:
    target: {type: string}
  required: [target]
run: "echo got {target}"
timeout_s: 30
"""


class TestLoadValidate:
    def test_valid(self, tmp_path):
        p = load_yaml_plugin(_mk_plugin(tmp_path, VALID))
        assert p.name == "getshell"

    def test_bad_name(self, tmp_path):
        with pytest.raises(PluginError):
            load_yaml_plugin(_mk_plugin(tmp_path, VALID.replace("getshell", "Bad Name", 1)))

    def test_template_param_not_in_schema(self, tmp_path):
        bad = VALID.replace("run: \"echo got {target}\"", "run: \"echo {nope}\"")
        with pytest.raises(PluginError, match="未在 schema"):
            load_yaml_plugin(_mk_plugin(tmp_path, bad))

    def test_required_not_in_template_allowed(self, tmp_path):
        # required 参数不必出现在命令模板（可为上下文信息；调用时仍必填校验）
        ok = VALID.replace("run: \"echo got {target}\"", "run: \"echo hi\"")
        p = load_yaml_plugin(_mk_plugin(tmp_path, ok))
        assert p.command == "echo hi"

    def test_missing_run(self, tmp_path):
        with pytest.raises(PluginError, match="run"):
            load_yaml_plugin(_mk_plugin(tmp_path, VALID.replace(
                "run: \"echo got {target}\"", "")))


class TestPluginFn:
    def _fn(self, tmp_path, run_line="printf 'shell:%s' {target}", timeout_s=30):
        text = VALID.replace("run: \"echo got {target}\"", f"run: \"{run_line}\"")
        text = text.replace("timeout_s: 30", f"timeout_s: {timeout_s}")
        return make_plugin_fn(load_yaml_plugin(_mk_plugin(tmp_path, text)))

    def test_args_quoted_against_injection(self, tmp_path):
        fn = self._fn(tmp_path)
        # 注入 payload 经 shlex.quote 后变成单参数，不会被 shell 解释（argv 执行）
        out = _run(fn({"target": "x; id; $(rm -rf /)"}))
        assert "shell:x; id; $(rm -rf /)" in out
        assert "uid=" not in out

    def test_unknown_param_rejected(self, tmp_path):
        fn = self._fn(tmp_path)
        out = _run(fn({"target": "a", "evil": "b"}))
        assert out.startswith("Error: 未知参数")

    def test_missing_required(self, tmp_path):
        fn = self._fn(tmp_path)
        out = _run(fn({}))
        assert out.startswith("Error: 缺少必填参数")

    def test_timeout(self, tmp_path):
        fn = self._fn(tmp_path, run_line="sleep 5", timeout_s=1)
        out = _run(fn({"target": "a"}))
        assert "timeout" in out

    def test_missing_binary_graceful(self, tmp_path):
        fn = self._fn(tmp_path, run_line="definitely-not-exists-bin {target}")
        out = _run(fn({"target": "a"}))
        assert out.startswith("Error: 命令") and "不存在" in out


class TestEngineIntegration:
    def test_extra_tool_available(self, tmp_path):
        """插件工具通过 extra_tools 进入 Engine 并出现在 schema。"""
        schema = {"type": "function", "function": {
            "name": "getshell", "description": "d",
            "parameters": {"type": "object", "properties": {"target": {"type": "string"}},
                           "required": ["target"]}}}
        calls = []

        async def fn(args):
            calls.append(args)
            return "shell ok"

        cfg = RunConfig(job_id="t", objective="o", system_extra="x", workspace=tmp_path)
        engine = Engine(cfg, llm=None, extra_tools={"getshell": (schema, fn)})
        assert "getshell" in engine.tools
        assert any(s["function"]["name"] == "getshell"
                   for s in engine.extra_tools.values() for s in [s[0]])

    def test_engine_tools_for_uses_installed_plugin(self, tmp_path):
        """engine_tools_for 只装配已安装插件；未安装名字静默跳过。"""
        plug_dir = tmp_path / "plugins"
        plug_dir.mkdir()
        _mk_plugin(plug_dir, VALID, "getshell.yaml")
        from secpt.tools.plugin_loader import discover_plugins
        yp, _ = discover_plugins([plug_dir])
        assert "getshell" in yp
        tools = engine_tools_for(["getshell", "nosuch"], dirs=[plug_dir])
        assert list(tools) == ["getshell"]
