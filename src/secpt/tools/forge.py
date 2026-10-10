"""ToolForge：让 LLM 自己写工具。

流程：
1. LLM 收到工具需求描述
2. 生成 Python 工具类代码
3. 静态安全校验（AST 检查）
4. 写入 challenges/<id>/tools/ 目录
5. 动态 import 并注册到 Registry
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from secpt.core.tools.base import Registry, Tool

if TYPE_CHECKING:
    from secpt.core.llm.base import BaseLLM

logger = structlog.get_logger(__name__)

# 工具代码模板
TOOL_TEMPLATE = '''"""Auto-generated tool: {name}"""

from secpt.core.tools.base import Tool


class {class_name}(Tool):
    name = "{name}"
    description = """{description}"""

    async def run(self, args: str) -> str:
{body}
'''


class ToolSecurityError(Exception):
    """工具代码安全检查失败。"""


class ToolForge:
    """工具锻造器：LLM 生成 → 安全校验 → 动态加载。"""

    # 禁止的 AST 节点
    _BANNED_NODES = (
        ast.Import,          # 禁止 import（除模板已含）
        ast.ImportFrom,      # 禁止 from-import
        ast.Exec,            # exec
        ast.Eval,            # eval
        ast.Global,          # global
        ast.Nonlocal,        # nonlocal
        ast.ClassDef,        # 嵌套类定义（只允许顶层一个）
        ast.AsyncFunctionDef, # 嵌套 async def（只允许顶层 run）
        ast.FunctionDef,     # 嵌套普通函数
        ast.Lambda,          # lambda（可绕过检查）
        ast.Yield,           # yield（生成器）
        ast.YieldFrom,       # yield from
    )

    # 禁止的调用名
    _BANNED_CALLS = {
        "eval", "exec", "compile", "__import__", "open", "input",
        "getattr", "setattr", "delattr", "globals", "locals", "vars",
        "type", "isinstance", "issubclass", "super",
        "breakpoint", "exit", "quit", "help", "dir",
        "id", "hash", "repr", "str", "bytes", "int", "float",
        "os.system", "subprocess", "system", "popen", "spawn",
        "socket", "requests", "urllib", "httpx", "aiohttp",
        "pickle", "marshal", "shelve", "dbm",
        "pathlib", "Path", "os.path", "shutil",
        "tempfile", "mkdtemp", "mktemp",
    }

    def __init__(self, llm: BaseLLM, tools_dir: Path | None = None) -> None:
        self._llm = llm
        self._tools_dir = tools_dir or Path("challenges/generated_tools")
        self._tools_dir.mkdir(parents=True, exist_ok=True)
        self._generated: dict[str, type[Tool]] = {}

    async def forge(self, spec: dict) -> type[Tool]:
        """根据规格生成工具。

        spec: {
            "name": "tool_name",       # 必需，蛇形命名
            "description": "...",       # 必需
            "body": "..."               # 必需，run 方法体（缩进 8 空格）
        }
        """
        name = spec.get("name", "").strip()
        description = spec.get("description", "").strip()
        body = spec.get("body", "").strip()

        if not name or not description or not body:
            raise ValueError("spec 必须包含 name / description / body")

        # 校验命名
        if not re.match(r'^[a-z][a-z0-9_]*$', name):
            raise ValueError(f"工具名必须蛇形命名: {name}")

        # 构造完整代码
        class_name = "".join(w.capitalize() for w in name.split("_")) + "Tool"
        code = TOOL_TEMPLATE.format(
            name=name,
            class_name=class_name,
            description=description.replace('"""', '\\"\\"\\"'),
            body=self._indent_body(body),
        )

        # 安全校验
        self._validate(code)

        # 写入文件
        tool_path = self._tools_dir / f"{name}.py"
        tool_path.write_text(code, encoding="utf-8")

        # 动态加载
        tool_cls = self._load_from_file(tool_path, class_name)
        self._generated[name] = tool_cls

        logger.info(
            "tool.forged",
            name=name,
            path=str(tool_path),
            code_lines=len(code.splitlines()),
        )
        return tool_cls

    def register_all(self, registry: Registry) -> None:
        """把所有生成的工具注册到 Registry。"""
        for name, cls in self._generated.items():
            registry.register(cls())
            logger.info("tool.registered", name=name)

    def _indent_body(self, body: str) -> str:
        """把方法体缩进到正确层级。"""
        lines = body.split("\n")
        indented = []
        for line in lines:
            if line.strip():
                indented.append("        " + line)
            else:
                indented.append("")
        return "\n".join(indented)

    def _validate(self, code: str) -> None:
        """AST 安全校验。"""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            raise ToolSecurityError(f"语法错误: {e}") from e

        # 检查节点
        for node in ast.walk(tree):
            if isinstance(node, self._BANNED_NODES):
                raise ToolSecurityError(
                    f"禁止的语法: {type(node).__name__} at line {node.lineno}"
                )

            # 检查函数调用
            if isinstance(node, ast.Call):
                func = node.func
                # 直接调用
                if isinstance(func, ast.Name) and func.id in self._BANNED_CALLS:
                    raise ToolSecurityError(
                        f"禁止的函数调用: {func.id} at line {node.lineno}"
                    )
                # 属性调用（如 os.system）
                if isinstance(func, ast.Attribute):
                    full_name = self._get_attr_name(func)
                    if full_name in self._BANNED_CALLS:
                        raise ToolSecurityError(
                            f"禁止的属性调用: {full_name} at line {node.lineno}"
                        )

            # 检查字符串中的危险模式
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for pattern in ["__import__", "eval(", "exec(", "os.system", "subprocess"]:
                    if pattern in node.value:
                        raise ToolSecurityError(
                            f"字符串包含危险模式: {pattern} at line {node.lineno}"
                        )

        logger.debug("tool.validated", code_lines=len(code.splitlines()))

    def _get_attr_name(self, node: ast.Attribute) -> str:
        """获取属性的完整名称。"""
        parts = [node.attr]
        while isinstance(node.value, ast.Attribute):
            node = node.value
            parts.append(node.attr)
        if isinstance(node.value, ast.Name):
            parts.append(node.value.id)
        return ".".join(reversed(parts))

    def _load_from_file(self, path: Path, class_name: str) -> type[Tool]:
        """从文件动态加载工具类。"""
        spec = importlib.util.spec_from_file_location(
            f"secpt.generated.{path.stem}", str(path)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载: {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module

        try:
            spec.loader.exec_module(module)
        except Exception as e:
            raise ImportError(f"模块执行失败: {e}") from e

        cls = getattr(module, class_name, None)
        if cls is None:
            raise ImportError(f"类 {class_name} 不存在于 {path}")

        if not issubclass(cls, Tool):
            raise TypeError(f"{class_name} 不是 Tool 子类")

        return cls


__all__ = ["ToolForge", "ToolSecurityError"]
