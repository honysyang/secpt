"""基础内置工具：时间查询与安全表达式计算。

calculate 使用 AST 白名单求值而非 eval：
- 仅允许四则运算、幂、取整和有限的数学函数
- 幂运算限制指数上限，防止 9**9**9**9 之类的资源耗尽攻击
"""

from __future__ import annotations

import ast
import datetime
import math
from typing import Any

from harness.tools.decorator import tool

# 幂运算指数上限：超出视为恶意表达式
_MAX_EXPONENT = 10000

_ALLOWED_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

_ALLOWED_UNARYOPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: lambda a: a,
    ast.USub: lambda a: -a,
}

_ALLOWED_FUNCS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}

_ALLOWED_NAMES: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}


def _eval_node(node: ast.expr) -> Any:
    """递归求值 AST 节点，遇到白名单之外的元素立即拒绝。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f"不支持的字面量类型: {type(node.value).__name__}")

    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        if isinstance(node.op, ast.Pow) and abs(_eval_node(node.right)) > _MAX_EXPONENT:
            raise ValueError(f"幂指数超出上限 {_MAX_EXPONENT}")
        return _ALLOWED_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))

    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ValueError("只允许调用白名单中的数学函数")
        if node.keywords:
            raise ValueError("不允许关键字参数")
        args = [_eval_node(arg) for arg in node.args]
        return _ALLOWED_FUNCS[node.func.id](*args)

    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]

    raise ValueError(f"不允许的表达式元素: {type(node).__name__}")


@tool
def get_time(format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """获取当前本地时间。

    Args:
        format: 时间格式（strftime 格式串）
    """
    return datetime.datetime.now().strftime(format)


@tool
def calculate(expression: str) -> str:
    """安全地计算数学表达式。

    支持四则运算、幂(**)、整除(//)、取模(%)、数学函数
    (abs/round/min/max/pow/sqrt/log/log10/exp/sin/cos/tan)
    以及常量 pi、e、tau、inf。

    Args:
        expression: 数学表达式，例如 "(2+3)*4 / sqrt(16)"
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        return f"计算失败: {exc}"
    return str(result)
