"""local_runner 单测：argv 执行 / env 清理 / rlimit / 截断落盘 / audit。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from secpt.runtime.local_runner import _clean_env, register, run_local

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="rlimit 仅 POSIX")


@pytest.fixture(autouse=True)
def _isolated_work(monkeypatch, tmp_path: Path):
    """隔离 SECPT_WORK_DIR / SECPT_VAR，避免测试污染真实 var/。"""
    monkeypatch.setenv("SECPT_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    yield


def test_run_local_basic_exec_and_output_file(tmp_path: Path) -> None:
    out = run_local(["echo", "hello-local"])
    assert "rc=0" in out and "hello-local" in out
    # 完整输出落盘 outputs/local-1.log
    log_files = list((tmp_path / "outputs").glob("local-*.log"))
    assert len(log_files) == 1 and "hello-local" in log_files[0].read_text(encoding="utf-8")


def test_clean_env_strips_secrets_keeps_task_vars(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me-not")
    monkeypatch.setenv("SECPT_BUDGET_TOKENS", "999")  # 任务变量透传（含 TOKEN 也保留）
    monkeypatch.setenv("BENCHMARK_TOKEN", "bench-tok")  # benchmark 必需任务凭据
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _clean_env()
    assert "DEEPSEEK_API_KEY" not in env  # 宿主凭据不进子进程
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["SECPT_BUDGET_TOKENS"] == "999"  # SECPT_* 命名空间整段透传
    assert env["BENCHMARK_TOKEN"] == "bench-tok"


def test_child_sees_clean_env_only(tmp_path: Path) -> None:
    import os

    os.environ["LEAK_ME_SECRET"] = "nope"
    out = run_local(["python3", "-c", "import os;print('SECRET' in os.environ)"])
    assert "False" in out
    os.environ.pop("LEAK_ME_SECRET", None)


def test_rlimit_cpu_kills_long_task(tmp_path: Path) -> None:
    """CPU 硬限：死循环进程 1s 内被 RLIMIT_CPU 杀掉（SIGKILL，rc!=0）。"""
    out = run_local(["python3", "-c", "while True: pass"], timeout_s=30, cpu_s=1, mem_mb=512)
    assert "rc=0" not in out  # 未正常结束
    assert "完整输出=" in out


def test_output_truncated_and_full_file(tmp_path: Path) -> None:
    big = "x" * 20000
    out = run_local(["python3", "-c", f"print('{big}')"])
    assert len(out) < 12000  # 截断片段 < 8KB 头+正文
    full = next(iter((tmp_path / "outputs").glob("local-*.log"))).read_text(encoding="utf-8")
    assert len(full) >= 20000  # 完整输出落盘


def test_timeout_kills_and_notes(tmp_path: Path) -> None:
    out = run_local(["sleep", "10"], timeout_s=1, cpu_s=30)
    assert "timeout" in out and "rc=0" not in out


def test_missing_binary_actionable_error() -> None:
    out = run_local(["no-such-tool-xyz"])
    assert out.startswith("Error:") and "不存在" in out


def test_register_schema_contract() -> None:
    tools = register()
    assert "local_runner" in tools
    schema, fn = tools["local_runner"]
    assert schema["type"] == "object" and schema["properties"]["command"]["type"] == "array"
    assert callable(fn)


def test_fn_rejects_non_argv(monkeypatch) -> None:
    import asyncio

    _tools = register()
    fn = _tools["local_runner"][1]
    for bad in (None, "ls -la", [42], [], ["ls", ""]):
        out = asyncio.run(fn({"command": bad}))
        assert out.startswith("Error:")
