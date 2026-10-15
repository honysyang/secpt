"""local_runner：带资源限制的本地命令执行器（Python 插件形态）。

与 bash 工具的区别与定位：
- argv 数组直接执行（subprocess.run, shell=False）——无 shell 元字符面，
  天然免疫注入；参数即列表，模型无需 shell 转义
- 资源硬限制（RLIMIT_CPU / RLIMIT_AS / RLIMIT_NPROC / RLIMIT_FSIZE）——
  暴力破解/符号执行等长任务在本地只能 small-input dry-run，超限即杀
- 环境最小化：仅透传白名单变量 + SECPT_*/BENCHMARK_* 任务变量；
  *KEY/*TOKEN/*SECRET/*PASSWORD 类变量一律剥离（防借 env 读凭据）
- 输出治理：截断片段给模型，完整输出落 <work_dir>/outputs/local-N.log
- 审计：每次执行写 var/audit/<job_id>.jsonl（local_runner.exec 事件）

装配方式：tools/plugins 目录由 plugin_loader 发现（register() 形态），
profile 的 tools 显式声明 local_runner 才装配（admin enable 语义）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import subprocess
import time
from pathlib import Path

OUT_CAP = 8000  # 给模型的截断片段上限（与 bash 工具一致）

# 环境白名单：通用必要变量；任务变量走 SECPT_*/BENCHMARK_ 前缀命名空间整段透传
_ENV_KEEP = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "PWD", "SHELL"}


def _audit_path() -> Path:
    job_id = os.environ.get("SECPT_JOB_ID", "")
    base = Path(os.environ.get("SECPT_VAR", "")) if os.environ.get("SECPT_VAR") else Path("var")
    if job_id:
        return base / "audit" / f"{job_id}.jsonl"
    return base / "audit" / "plugins.jsonl"


def _audit(event: str, **fields: object) -> None:
    """写审计事件（复用 AuditLogger；失败不阻断执行）。"""
    try:
        from secpt.governance.audit import AuditLogger

        AuditLogger(str(_audit_path())).log(event, **fields)
    except Exception:
        pass


def _clean_env() -> dict[str, str]:
    """最小化环境：白名单 + SECPT_*/BENCHMARK_* 任务变量。

    语义边界：SECPT_/BENCHMARK_ 前缀是项目自定义任务命名空间（含 BENCHMARK_TOKEN
    等必需任务凭据），整段透传不做后缀剥离；其余未列名变量（含 *_API_KEY 等宿主
    凭据）一律不进入子进程——凭据隔离靠白名单本身，而非后缀黑名单。
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k in _ENV_KEEP or k.startswith(("SECPT_", "BENCHMARK_"))
    }


def _make_preexec(cpu_s: int, mem_mb: int) -> object | None:
    """构造子进程 rlimit 预设（POSIX；非 POSIX 平台返回 None 跳过限制）。"""
    if not hasattr(resource, "setrlimit"):
        return None

    def _apply() -> None:  # pragma: no cover — 子进程内执行，进程外无法直接断言
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        mem = mem_mb * 1024 * 1024
        with contextlib.suppress(OSError, ValueError):
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        with contextlib.suppress(OSError, ValueError):
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        with contextlib.suppress(OSError, ValueError):
            resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)

    return _apply


def _next_output_path(work_dir: Path) -> Path:
    out_dir = work_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (out_dir / f"local-{n}.log").exists():
        n += 1
    return out_dir / f"local-{n}.log"


def run_local(
    argv: list[str],
    *,
    timeout_s: int = 300,
    cpu_s: int = 60,
    mem_mb: int = 1024,
    audit: bool = True,
) -> str:
    """同步执行入口（analyze_binary 等编排层复用）。

    argv 数组执行、rlimit 硬限、env 最小化；完整输出落 outputs/local-N.log，
    截断片段返回给调用方。错误信息可行动（说明限制值与完整输出路径）。
    """
    work_dir = Path(os.environ.get("SECPT_WORK_DIR", Path.cwd()))
    started = time.monotonic()
    full_log = _next_output_path(work_dir)
    rc: int | None = None
    err = ""
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            capture_output=True,
            timeout=timeout_s,
            cwd=str(work_dir),
            env=_clean_env(),
            preexec_fn=_make_preexec(cpu_s, mem_mb),
        )
        rc, text = (
            proc.returncode,
            (proc.stdout + (b"\n" + proc.stderr if proc.stderr else b"")).decode(errors="replace"),
        )
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or b"") + (b"\n" + e.stderr if e.stderr else b"")
        text = text.decode(errors="replace") if isinstance(text, bytes) else str(text)
        err = f"timeout {timeout_s}s（进程已终止）"
    except FileNotFoundError:
        return f"Error: '{argv[0]}' 不存在。请确认工具已安装（可用 bash which 查询）"
    except PermissionError as e:
        return f"Error: 无执行权限: {e}"
    except Exception as e:
        return f"Error: 启动失败: {e}"

    dur = round(time.monotonic() - started, 2)
    full_log.write_text(text or "", encoding="utf-8")
    if audit:
        _audit(
            "local_runner.exec",
            argv=[str(a)[:120] for a in argv],
            rc=rc,
            duration_s=dur,
            timeout=err or None,
            output_file=str(full_log),
            cpu_s=cpu_s,
            mem_mb=mem_mb,
        )

    head = f"[local_runner] rc={rc} 耗时={dur}s 完整输出={full_log}"
    if err:
        head += f"（{err}）"
    snippet = (text or "(no output)")[:OUT_CAP]
    return f"{head}\n{snippet}"


_LOCAL_RUNNER_SCHEMA = {
    "description": (
        "本地受控命令执行：argv 数组直接执行（无 shell），CPU/内存/文件大小硬限制，"
        "超限自动杀进程。适合 pwntools/python 脚本运行与短任务验证；长任务"
        "（爆破/符号执行）只能 small-input dry-run，生产请放专门 worker。"
        "完整输出落 outputs/local-N.log，仅截断片段返回。"
    ),
    "type": "object",
    "properties": {
        "command": {
            "type": "array",
            "items": {"type": "string"},
            "description": 'argv 数组，如 ["python3", "exploit.py", "--dry-run"]；不要传 shell 字符串',
        },
        "timeout_s": {
            "type": "integer",
            "description": "墙钟超时秒数（默认 300，上限 600）",
        },
    },
    "required": ["command"],
}


async def _fn(args: dict) -> str:
    args = args or {}
    argv = args.get("command")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) and a.strip() for a in argv)
    ):
        return 'Error: command 必须是非空字符串数组（argv 形态，如 ["python3","x.py"]）'
    if len(argv) > 64:
        return "Error: argv 过长（>64 项）"
    timeout_s = min(int(args.get("timeout_s") or 300), 600)
    return await asyncio.to_thread(run_local, argv, timeout_s=timeout_s, cpu_s=60, mem_mb=1024)


def register() -> dict[str, tuple[dict, object]]:
    """Python 插件注册点：{name: (schema, fn)}。"""
    return {"local_runner": (_LOCAL_RUNNER_SCHEMA, _fn)}


__all__ = ["_clean_env", "register", "run_local"]
