"""工具插件：把外部工具/其他智能体的能力接入 Engine。

两种形态：
1. 声明式 YAML（tools/plugins/*.yaml）——零代码集成 CLI 工具（推荐）：
     name: getshell
     description: ...
     schema: {type: object, properties: {...}, required: [...]}   # OpenAI function schema
     run: "getshell --target {target} --json"                      # 命令模板
     timeout_s: 300
2. Python（tools/plugins/*.py，暴露 register() -> {name: (schema, fn)}）——
   需要编程能力的工具；fn 为 async fn(args: dict) -> str。

安全边界：
- 模板参数经 shlex.quote 替换，argv 数组执行（禁 shell=True）
- schema 未声明的参数一律拒绝；required 缺失报错
- 超时杀进程、输出截断
- 插件工具只在其被 profile/solve 显式引用时装配，不自动全局开放
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

PLUGINS_DIR = Path(__file__).resolve().parents[3] / "tools" / "plugins"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_FIELD_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
OUT_CAP = 8000


@dataclass
class ToolPlugin:
    """一个已校验的插件工具。"""

    name: str
    description: str
    schema: dict  # OpenAI function schema（parameters 部分）
    command: str = ""  # YAML 插件的命令模板
    timeout_s: int = 120
    source: str = ""  # 文件路径（排障用）


class PluginError(Exception):
    """插件定义非法。"""


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------


def _validate(p: ToolPlugin) -> None:
    if not _NAME_RE.match(p.name):
        raise PluginError(f"插件名非法（小写字母/数字/下划线，字母开头）: {p.name!r}")
    if not isinstance(p.schema, dict) or p.schema.get("type") != "object":
        raise PluginError(f"{p.name}: schema 必须是 object")
    props = p.schema.get("properties") or {}
    if not isinstance(props, dict):
        raise PluginError(f"{p.name}: schema.properties 必须是 dict")
    required = p.schema.get("required") or []
    if not set(required) <= set(props):
        raise PluginError(f"{p.name}: required 必须是 properties 的子集")
    if p.command:
        # 模板里出现的参数必须已在 schema 声明（防拼错/注入面）；
        # required 参数不强制出现在模板里（可能是上下文信息，调用时仍必填校验）
        tpl_fields = set(_FIELD_RE.findall(p.command))
        if not tpl_fields <= set(props):
            raise PluginError(
                f"{p.name}: 模板参数 {sorted(tpl_fields - set(props))} 未在 schema.properties 声明"
            )


def load_yaml_plugin(path: Path) -> ToolPlugin:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PluginError(f"{path}: 不是合法 YAML 对象")
    p = ToolPlugin(
        name=str(data.get("name") or Path(path).stem),
        description=str(data.get("description") or f"插件工具 {Path(path).stem}"),
        schema=data.get("schema") or {"type": "object", "properties": {}},
        command=str(data.get("run") or ""),
        timeout_s=int(data.get("timeout_s") or 120),
        source=str(path),
    )
    if not p.command:
        raise PluginError(f"{path}: 缺少 run 命令模板")
    _validate(p)
    return p


def load_python_plugin(path: Path) -> dict[str, tuple[dict, object]]:
    """Python 插件：register() -> {name: (schema, fn)}。"""
    spec = importlib.util.spec_from_file_location(f"secpt_plugin_{Path(path).stem}", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{path}: 无法加载")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    register = getattr(mod, "register", None)
    if not callable(register):
        raise PluginError(f"{path}: 缺少 register()")
    tools = register()
    if not isinstance(tools, dict):
        raise PluginError(f"{path}: register() 必须返回 dict")
    for name, pair in tools.items():
        if not _NAME_RE.match(name):
            raise PluginError(f"插件工具名非法: {name!r}")
        schema, fn = pair
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise PluginError(f"{name}: schema 必须是 object")
        if not callable(fn):
            raise PluginError(f"{name}: fn 不可调用")
    return tools


def discover_plugins(
    dirs: list[Path] | None = None,
) -> tuple[dict[str, ToolPlugin], dict[str, tuple[dict, object]]]:
    """扫描插件目录，返回 (yaml_plugins, python_tools)。非法插件跳过并告警。"""
    dirs = dirs or [PLUGINS_DIR]
    yaml_plugins: dict[str, ToolPlugin] = {}
    python_tools: dict[str, tuple[dict, object]] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
            try:
                p = load_yaml_plugin(f)
                yaml_plugins[p.name] = p
            except PluginError as e:
                print(f"[plugins] 跳过非法插件 {f.name}: {e}")
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                python_tools.update(load_python_plugin(f))
            except Exception as e:
                print(f"[plugins] 跳过非法插件 {f.name}: {e}")
    return yaml_plugins, python_tools


# ---------------------------------------------------------------------------
# YAML 插件 → Engine 工具
# ---------------------------------------------------------------------------


def make_plugin_fn(plugin: ToolPlugin):
    """生成 async fn(args)->str：模板替换（shlex.quote）→ argv 执行。"""
    props = set((plugin.schema.get("properties") or {}).keys())

    async def fn(args: dict) -> str:
        args = args or {}
        unknown = set(args) - props
        if unknown:
            return f"Error: 未知参数 {sorted(unknown)}，可用：{sorted(props)}"
        missing = [f for f in (plugin.schema.get("required") or []) if args.get(f) in (None, "")]
        if missing:
            return f"Error: 缺少必填参数 {missing}"

        def _sub(m: re.Match) -> str:
            v = args.get(m.group(1), "")
            return shlex.quote(str(v))

        rendered = _FIELD_RE.sub(_sub, plugin.command)
        try:
            argv = shlex.split(rendered)
        except ValueError as e:
            return f"Error: 命令渲染失败: {e}"
        if not argv:
            return "Error: 空命令"
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            return (
                f"Error: 命令 '{argv[0]}' 不存在。插件 {plugin.name} 需要的外部工具"
                f"未安装（定义于 {plugin.source}）"
            )
        except Exception as e:
            return f"Error: 启动失败: {e}"
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=plugin.timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return f"Error: timeout {plugin.timeout_s}s"
        text = (out_b + (b"\n" + err_b if err_b else b"")).decode(errors="replace")
        return text[:OUT_CAP] if text.strip() else "(no output)"

    return fn


def engine_tools_for(
    names: list[str], dirs: list[Path] | None = None
) -> dict[str, tuple[dict, object]]:
    """按名字装配插件工具 -> {name: (schema, fn)}（engine 的 extra_tools 格式）。"""
    yaml_plugins, python_tools = discover_plugins(dirs)
    out: dict[str, tuple[dict, object]] = {}
    for name in names:
        if name in yaml_plugins:
            p = yaml_plugins[name]
            schema = {
                "type": "function",
                "function": {"name": p.name, "description": p.description, "parameters": p.schema},
            }
            out[name] = (schema, make_plugin_fn(p))
        elif name in python_tools:
            schema, fn = python_tools[name]
            full = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema.get("description") or f"插件工具 {name}",
                    "parameters": schema,
                },
            }
            out[name] = (full, fn)
        # 未命中：返回时不含该名字（调用方决定是否并入 bash 白名单）
    return out
