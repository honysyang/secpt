"""S2 worker：独立进程执行一个 job。

用法：python -m secpt.core.job.worker --job-id <id>

职责：
- 加载 task.yaml → 重建 RunConfig（含 resume：从最新 round 继续）
- 建 workspace（var/jobs/<job_id>/ws/）
- 起 Engine（挂 EventSink hooks + 全局限速）
- 心跳：每 10s 更新 jobs.updated_at（供 manager stuck 检测）
- SIGTERM → 优雅退出（当前 round 已由 on_round_end 落盘，resume 兜底）

退出码：0 succeeded / 1 failed / 2 budget_exceeded / 3 error

红线：不 import manager；只认 job_id。
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

# 允许 `python -m secpt.core.job.worker` 直接运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from secpt.core.job.event import EventSink
from secpt.core.job.models import JobRecord
from secpt.core.job.resume import jobs_dir, make_run_config, read_task_snapshot
from secpt.core.job.rounds import load_latest_round
from secpt.core.job.store import JobStore
from secpt.core.profile.render import load_prior as _load_prior
from secpt.core.profile.render import render_system_extra as _rse
from secpt.core.watchdog import Watchdog
from secpt.governance.ratelimit import GlobalRateLimiter
from secpt.runtime import engine as _engine

HEARTBEAT_S = 10.0
# SIGTERM 后给 engine 的收尾窗口：覆盖最长 LLM 调用（180s）+ 落盘窗口，到点才强杀。
# WorkerCancelled 在 on_step（步间）抛出，round 快照本已无损；宽窗口只为防 engine
# 卡在单次长 LLM 调用里被 3s 强杀截断（7×24 长任务丢尾代价高）。
CANCEL_GRACE_S = 240.0

_stop = threading.Event()


class WorkerCancelled(Exception):
    """SIGTERM 触发：下一次 on_step 时抛给 engine，中断主循环。"""


class _CancelHook:
    """包一层 EventSink：_stop 置位后下一次 on_step 抛 WorkerCancelled。

    其余 hooks 原样委托给 inner（engine 按 hasattr 探测，不感知包装层）。
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def on_step(self, steps: int, elapsed: float) -> None:
        if _stop.is_set():
            raise WorkerCancelled()
        self._inner.on_step(steps, elapsed)


def _handle_sigterm(signum, frame):
    """SIGTERM → 置 _stop：on_step 下次检查即中断 engine，watcher 兜底强杀。"""
    print("[worker] SIGTERM received, stopping...")
    _stop.set()


def _safe_unlink(p: Path) -> None:
    with contextlib.suppress(OSError):
        p.unlink(missing_ok=True)


def _cancel_watcher(pid_file: Path) -> None:
    """兜底：SIGTERM 后 engine 若迟迟出不来（hang 死而非长调用），到点强杀（130=128+SIGTERM）。

    正常路径：_stop 置位 → on_step 抛 WorkerCancelled → engine 退出（≤1 步）。
    宽窗口只在 engine 真正卡死时才会走到强杀。
    """
    _stop.wait()
    time.sleep(CANCEL_GRACE_S)
    print("[worker] cancel grace elapsed, force exit (130)")
    _safe_unlink(pid_file)
    os._exit(130)


def _heartbeat(store: JobStore, job_id: str, interval: float) -> None:
    """后台心跳：每 interval 秒 touch jobs.updated_at。"""
    while not _stop.is_set():
        time.sleep(interval)
        try:
            rec = store.get_job(job_id)
            if rec is not None:
                rec.touch()
                store.upsert_job(rec)
        except Exception as e:
            print(f"[worker] heartbeat failed: {e}")


def run_worker(job_id: str, *, max_parallel_rps: float = 10.0) -> int:
    """执行 job，返回退出码。"""
    from secpt.runtime.llm.client import call_llm

    jdir = jobs_dir() / job_id
    task_yaml = jdir / "task.yaml"
    if not task_yaml.is_file():
        print(f"[worker] job {job_id} 无 task.yaml", file=sys.stderr)
        return 3

    # worker.pid：供 manager 跨进程 cancel 定位进程（退出时清理）
    pid_file = jdir / "worker.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: _safe_unlink(pid_file))

    spec, run_cfg = read_task_snapshot(jdir)
    db_path = run_cfg.get("db_path", "var/secpt.sqlite")
    store = JobStore(db_path)
    n, messages = load_latest_round(job_id, jobs_dir())
    cfg = make_run_config(job_id, spec, run_cfg)

    if messages:
        cfg.initial_messages = messages
        cfg.round_offset = n
    else:
        # 无存档 → 从头开始（重建 system）
        cfg.system_extra = _rse(
            spec.task_type,
            objective=spec.objective,
            tools_doc=_engine.build_tools_doc(cfg),
            prior=_load_prior(spec.task_type),
        )

    # 独立 workspace（S2 红线：多 job 绝不共用 ws）
    ws = jdir / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    cfg.workspace = ws

    # 全局限速（跨 job 生效）
    cfg.ratelimit = GlobalRateLimiter(
        db_path=db_path,
        max_rps=max_parallel_rps,
    )

    # 事件流 + 状态
    sink = EventSink(job_id, jobs_dir(), store=store)
    rec = store.get_job(job_id) or JobRecord(job_id=job_id, spec=spec, status="running")
    rec.set_status("running")
    store.upsert_job(rec)
    sink.status_change("running", round_=n)

    # 心跳线程
    hb = threading.Thread(target=_heartbeat, args=(store, job_id, HEARTBEAT_S), daemon=True)
    hb.start()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    # 兜底：engine 卡死/长 LLM 调用时到点强杀
    threading.Thread(target=_cancel_watcher, args=(pid_file,), daemon=True).start()

    llm = call_llm
    if os.environ.get("SECPT_FAKE_LLM"):
        # 测试用假 LLM：n-1 次工具调用后输出 ANSWER flag（留 1 步收尾）
        from secpt.core.job.fakellm import FakeLLM

        llm = FakeLLM(n_steps=max(1, cfg.max_steps - 1), job_id=job_id)

    # 快照里的插件工具重新装配（插件定义可能已更新）
    from secpt.tools.plugin_loader import engine_tools_for

    extra_tools = engine_tools_for(run_cfg.get("extra_tool_names") or [])

    exit_code = 3
    cancelled = False
    try:
        # S3：sink 外包 watchdog（空转检测 → paused + 换层提示），再包 cancel 钩子
        watchdog = Watchdog(sink, store=store, job_id=job_id)
        result = asyncio.run(
            _engine.Engine(
                cfg, llm=llm, hooks=_CancelHook(watchdog), extra_tools=extra_tools or None
            ).run()
        )
        cancelled = _stop.is_set()  # 在 finally 置位前捕获取消标志
    except Exception as e:
        print(f"[worker] engine fatal: {e}", file=sys.stderr)
        rec = store.get_job(job_id) or rec
        if _stop.is_set():
            rec.set_status("cancelled")
            store.upsert_job(rec)
            return 130
        rec.set_status("failed", error=str(e))
        store.upsert_job(rec)
        sink.error(str(e))
        return 3
    finally:
        _stop.set()

    rec = store.get_job(job_id) or rec
    rec.steps = result.steps
    if cancelled:
        # 被 cancel：manager 已置 cancelled，这里保持不再覆盖
        rec.set_status("cancelled")
        exit_code = 130
    elif result.flag:
        rec.set_status("succeeded")
        sink.finding("flag", "flag", result.flag, "engine result")
        exit_code = 0
    elif result.status == "error":
        rec.set_status("failed", error="engine error")
        exit_code = 3
    elif result.status in ("budget_exceeded", "step_exceeded", "wallclock_exceeded"):
        # 精确语义：超步数≠超预算≠墙钟到点（RunResult.status 已区分）
        rec.set_status(result.status)
        exit_code = 2
    else:
        rec.set_status("failed")
        exit_code = 1
    store.upsert_job(rec)
    sink.status_change(rec.status)

    print(f"[worker] job={job_id} status={rec.status} flag={result.flag or ''}")
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(description="secpt job worker")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--max-rps", type=float, default=10.0)
    args = parser.parse_args()
    sys.exit(run_worker(args.job_id, max_parallel_rps=args.max_rps))


if __name__ == "__main__":
    main()
