"""安全的命令执行边界（白名单 subprocess）。

约束：
- 仅允许 :class:`secpt.config.models.ToolsConfig.whitelist` 内的命令基名
- 严禁 ``shell=True``，强制 ``asyncio.create_subprocess_exec`` + 参数数组
- 超时即 ``proc.kill()`` 并抛 :class:`TimeoutExpired`
- 全程 structlog 审计（含 ``whitelist_hit=True`` 标志）
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
import uuid
from dataclasses import dataclass, field

import structlog

from secpt.config.models import ToolsConfig
from secpt.config.settings import get_settings
from secpt.governance.audit import AuditLogger

logger = structlog.get_logger(__name__)

# 模块级共享审计器（懒初始化，首次使用时创建）
_audit_logger: AuditLogger | None = None


def _get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        audit_path = "./data/audit"
        try:
            audit_cfg = getattr(get_settings(), "audit", None)
            if audit_cfg is not None:
                audit_path = getattr(audit_cfg, "path", audit_path)
        except Exception:
            pass
        _audit_logger = AuditLogger(f"{audit_path}/subprocess_audit.jsonl")
    return _audit_logger


class SubprocessError(Exception):
    """subprocess 边界所有错误的基类。"""


class WhitelistError(SubprocessError):
    """命令不在白名单内，拒绝执行。"""


class TimeoutExpired(SubprocessError):
    """命令执行超时（已 kill）。"""


@dataclass
class ExecResult:
    """一次白名单命令的执行结果。"""

    command: str
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    audit_id: str | None = None
    whitelist_hit: bool = field(default=True, repr=False)


def _resolve_config(config: ToolsConfig | None) -> ToolsConfig:
    return config if config is not None else get_settings().tools


def _validate_whitelist(cmd: str, config: ToolsConfig) -> None:
    """仅匹配基名（basename），避免路径穿透；不在白名单直接抛。"""
    base = cmd.rsplit("/", 1)[-1]
    if base not in config.whitelist:
        raise WhitelistError(f"命令 '{base}' 不在白名单内 (whitelist={config.whitelist})，拒绝执行")


async def run_whitelisted(
    cmd: str,
    args: list[str] | tuple[str, ...] | None = None,
    *,
    config: ToolsConfig | None = None,
    timeout: float | None = None,
    audit_id: str | None = None,
) -> ExecResult:
    """白名单内执行外部命令（异步）。

    :param cmd: 命令基名或绝对路径（仅用基名做白名单匹配）
    :param args: 参数数组（不要拼成字符串）
    :param config: 工具配置；None 时从 :func:`get_settings` 读取
    :param timeout: 秒；None 用 ``config.default_timeout``
    :param audit_id: 审计关联 ID；None 时自动生成 uuid4
    :raises WhitelistError: 命令不在白名单
    :raises TimeoutExpired: 超时（已 kill）
    """
    cfg = _resolve_config(config)
    argv = list(args) if args is not None else []
    _validate_whitelist(cmd, cfg)

    timeout_val = timeout if timeout is not None else cfg.default_timeout
    command_str = shlex.join([cmd, *argv])

    # audit_id 自动生成，确保每次执行都有审计记录
    if audit_id is None:
        audit_id = uuid.uuid4().hex[:12]

    _get_audit_logger().log(
        "subprocess.exec.start",
        audit_id=audit_id,
        command=command_str,
        timeout=timeout_val,
    )

    logger.debug(
        "subprocess.exec.start",
        command=command_str,
        timeout=timeout_val,
        audit_id=audit_id,
    )

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SubprocessError(f"命令不存在: {cmd}") from exc
    except OSError as exc:
        raise SubprocessError(f"无法启动子进程: {exc}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_val)
    except TimeoutError as exc:
        proc.kill()
        # 等待进程真正退出，避免僵尸
        with contextlib.suppress(Exception):
            await proc.wait()
        duration_ms = (time.monotonic() - start) * 1000
        _get_audit_logger().log(
            "subprocess.exec.timeout",
            audit_id=audit_id,
            command=command_str,
            duration_ms=duration_ms,
            timeout=timeout_val,
        )
        logger.warning(
            "subprocess.exec.timeout",
            command=command_str,
            timeout=timeout_val,
            duration_ms=duration_ms,
            audit_id=audit_id,
            whitelist_hit=True,
        )
        raise TimeoutExpired(f"命令执行超时 ({timeout_val}s) 已 kill: {command_str}") from exc

    duration_ms = (time.monotonic() - start) * 1000
    returncode = proc.returncode if proc.returncode is not None else -1
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    _get_audit_logger().log(
        "subprocess.exec.done",
        audit_id=audit_id,
        command=command_str,
        returncode=returncode,
        duration_ms=duration_ms,
    )

    logger.info(
        "subprocess.exec.done",
        command=command_str,
        returncode=returncode,
        duration_ms=duration_ms,
        audit_id=audit_id,
        whitelist_hit=True,
    )

    return ExecResult(
        command=cmd,
        args=argv,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        audit_id=audit_id,
    )


def run_whitelisted_sync(
    cmd: str,
    args: list[str] | tuple[str, ...] | None = None,
    *,
    config: ToolsConfig | None = None,
    timeout: float | None = None,
    audit_id: str | None = None,
) -> ExecResult:
    """同步壳，内部 ``asyncio.run``；供 CLI / 脚本使用。"""
    return asyncio.run(
        run_whitelisted(cmd, args, config=config, timeout=timeout, audit_id=audit_id)
    )


__all__ = [
    "ExecResult",
    "SubprocessError",
    "TimeoutExpired",
    "WhitelistError",
    "run_whitelisted",
    "run_whitelisted_sync",
]
