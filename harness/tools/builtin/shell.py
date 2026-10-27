"""命令执行内置工具。

安全设计（对应工程规范"外部命令一律 argv 列表执行，绝不使用 shell=True"）：
- 字符串命令经 shlex.split 转为 argv，全程不经过 shell 解释
- argv 模式下 shell 元字符本身不构成注入，但仍拒绝换行/空字节等控制字符，
  防止参数伪造与日志注入
- 两类工具均标记 dangerous=True，配合 HITL 走人工确认
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
from typing import Any

from harness.logging.logger import get_logger
from harness.tools.decorator import tool

logger = get_logger(__name__)

# 命令参数中禁止出现的控制字符
_FORBIDDEN_CHARS = {"\n", "\r", "\x00"}
_DEFAULT_TIMEOUT = 30.0


def _validate_argv(argv: list[str]) -> None:
    """校验 argv：每个参数不含控制字符（空列表合法，如无参数的脚本调用）。"""
    for arg in argv:
        if any(ch in _FORBIDDEN_CHARS for ch in arg):
            raise ValueError(f"参数包含禁止的控制字符(换行/空字节): {arg!r}")


def _to_argv(command: str | list[str]) -> list[str]:
    """把字符串命令安全转为 argv 列表；列表输入原样校验。"""
    if isinstance(command, str):
        argv = shlex.split(command)
        if not argv:
            raise ValueError("命令不能为空")
    else:
        argv = [str(arg) for arg in command]
    _validate_argv(argv)
    return argv


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    """统一命令输出格式，便于 LLM 阅读。"""
    parts = [f"exit_code: {returncode}"]
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    return "\n".join(parts)


# 日志还原命令意图所需的最小完整性：URL 等关键参数通常在末尾，
# 截断过狠会导致事后无法审计实际请求目标
_ARGV_LOG_LIMIT = 16
_ARGV_LOG_MAX_CHARS = 200


def _argv_for_log(argv: list[str]) -> list[str]:
    """日志用 argv：参数数量与单参数长度做上限控制，防日志爆炸。"""
    return [arg[:_ARGV_LOG_MAX_CHARS] for arg in argv[:_ARGV_LOG_LIMIT]]


async def _run(argv: list[str], timeout: float, cwd: str | None) -> str:
    """异步执行 argv 命令（to_thread 包装同步 subprocess）。"""

    def _sync() -> tuple[int, str, str]:
        import subprocess

        proc = subprocess.run(  # noqa: S603 -- argv 列表执行，无 shell
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return proc.returncode, proc.stdout, proc.stderr

    try:
        returncode, stdout, stderr = await asyncio.to_thread(_sync)
    except subprocess.TimeoutExpired:
        return f"错误: 命令超时（>{timeout}s）"
    except FileNotFoundError:
        return f"错误: 命令不存在 {argv[0]}"
    except PermissionError:
        return f"错误: 无执行权限 {argv[0]}"
    return _format_result(returncode, stdout, stderr)


@tool(dangerous=True)
async def run_command(
    command: str | list[str],
    timeout: float = _DEFAULT_TIMEOUT,
    cwd: str | None = None,
) -> str:
    """执行单个系统命令并返回 stdout/stderr/退出码。

    命令以参数列表方式执行，不经过 shell 解释。
    仅支持"一个程序 + 它的参数"，如 ["curl", "-sSI", "https://example.com"]；
    管道(|)、重定向(>)、分号串联、&&、for/while 循环均不支持——
    需要组合逻辑时，把逻辑写入脚本后用 python_script 执行。

    Args:
        command: 命令字符串（自动分词）或参数列表，如 ["ls", "-la"]
        timeout: 超时秒数
        cwd: 工作目录
    """
    # 复合命令预检：宁可明确拒绝，也不静默跑出错误语义的结果。
    # shlex.split 会把 "echo a; ls" 拆成 echo + 两个垃圾参数（exit 0 但语义全错），
    # 这类静默错误比直接失败危害更大。
    if isinstance(command, str):
        for meta in (";", "&&", "||", "|", ">", "<", "$(", "`", "\n"):
            if meta in command:
                return (
                    f"错误: 不支持 shell 语法 {meta!r}（命令不经 shell 解释）。"
                    "请拆成多次 run_command 调用；"
                    "若需要管道/循环/组合逻辑，写脚本后用 python_script 执行"
                )

    try:
        argv = _to_argv(command)
    except ValueError as exc:
        return f"错误: {exc}"

    if shutil.which(argv[0]) is None:
        return f"错误: 命令不在 PATH 中: {argv[0]}"

    logger.info("tool_run_command", argv=_argv_for_log(argv))
    return await _run(argv, timeout, cwd)


@tool(dangerous=True)
async def python_script(
    path: str,
    args: list[str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """用当前 Python 解释器执行用户脚本文件。

    Args:
        path: 脚本路径（.py 文件）。纯文件名（如 scan.py）会在 outputs/ 目录下查找；
              指定子目录或绝对路径时按原路径查找
        args: 传给脚本的参数列表
        timeout: 超时秒数
    """
    from harness.tools.builtin.fs import resolve_output_path

    script_path, _redirected = resolve_output_path(path)

    argv = [sys.executable, str(script_path), *(args or [])]
    try:
        _validate_argv(argv[2:])
    except ValueError as exc:
        return f"错误: {exc}"

    if not script_path.is_file():
        return f"错误: 脚本不存在 {script_path}"

    logger.info("tool_python_script", path=str(script_path), args=args)
    return await _run(argv, timeout, cwd=None)
