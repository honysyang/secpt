"""白名单 subprocess 边界测试。

覆盖：
- 正常 echo 命令：returncode == 0，stdout 含内容
- 非白名单命令：抛 WhitelistError（含路径形式）
- 超时：sleep 5 + timeout=0.2 → TimeoutExpired，进程被 kill
- 审计日志：structlog 事件已发出（whitelist_hit=True）
- 自检：AST 中不得出现 ``shell=True`` 关键字参数
"""

from __future__ import annotations

import ast
import inspect

import pytest

from secpt.config.models import ToolsConfig
from secpt.runtime.subprocess import (
    ExecResult,
    SubprocessError,
    TimeoutExpired,
    WhitelistError,
    run_whitelisted,
    run_whitelisted_sync,
)


WHITELIST = ToolsConfig(whitelist=["echo", "true", "sleep"], default_timeout=30.0)


def test_import_ok() -> None:
    """导入 + 基类关系。"""
    assert issubclass(WhitelistError, SubprocessError)
    assert issubclass(TimeoutExpired, SubprocessError)


@pytest.mark.asyncio
async def test_echo_success() -> None:
    """白名单内 echo 正常返回。"""
    res = await run_whitelisted("echo", ["hello"], config=WHITELIST)
    assert isinstance(res, ExecResult)
    assert res.returncode == 0
    assert "hello" in res.stdout
    assert res.stderr == ""
    assert res.duration_ms >= 0
    assert res.whitelist_hit is True


@pytest.mark.asyncio
async def test_whitelist_rejects_unknown() -> None:
    """非白名单命令直接拒绝，不执行。"""
    with pytest.raises(WhitelistError, match="rm"):
        await run_whitelisted("rm", ["-rf", "/tmp/x"], config=WHITELIST)


@pytest.mark.asyncio
async def test_whitelist_rejects_path_bypass() -> None:
    """路径形式的 rm 也应按基名拦截。"""
    with pytest.raises(WhitelistError):
        await run_whitelisted("/bin/rm", ["-rf", "/tmp/x"], config=WHITELIST)


@pytest.mark.asyncio
async def test_timeout_kill() -> None:
    """sleep 5 配 timeout=0.2 抛 TimeoutExpired。"""
    with pytest.raises(TimeoutExpired):
        await run_whitelisted("sleep", ["5"], config=WHITELIST, timeout=0.2)


@pytest.mark.asyncio
async def test_audit_log_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计事件通过 structlog 发出：monkeypatch logger 捕获事件字典。"""
    from secpt.runtime import subprocess as sp

    events: list[dict[str, object]] = []

    class _CaptureLogger:
        def info(self, event: str, **kw: object) -> None:
            events.append({"event": event, **kw})

        def warning(self, event: str, **kw: object) -> None:
            events.append({"event": event, **kw})

        def debug(self, event: str, **kw: object) -> None:
            events.append({"event": event, **kw})

    monkeypatch.setattr(sp, "logger", _CaptureLogger())

    await run_whitelisted("echo", ["audited"], config=WHITELIST, audit_id="test-audit-2")

    assert any(e.get("event") == "subprocess.exec.done" for e in events)
    done = next(e for e in events if e.get("event") == "subprocess.exec.done")
    assert done.get("whitelist_hit") is True
    assert done.get("audit_id") == "test-audit-2"
    assert "echo" in str(done.get("command"))
    assert done.get("returncode") == 0
    assert isinstance(done.get("duration_ms"), float)


def test_sync_wrapper() -> None:
    """同步壳可调用。"""
    res = run_whitelisted_sync("echo", ["sync"], config=WHITELIST)
    assert res.returncode == 0
    assert "sync" in res.stdout


def test_no_shell_true_keyword_in_ast() -> None:
    """自检：AST 中任何调用都不允许 ``shell=True`` 关键字参数。"""
    from secpt.runtime import subprocess as sp

    src = inspect.getsource(sp)
    tree = ast.parse(src)
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell":
                    offenders.append(f"line {node.lineno}: shell={ast.unparse(kw.value)}")

    assert not offenders, f"subprocess.py 不得使用 shell= 参数: {offenders}"
