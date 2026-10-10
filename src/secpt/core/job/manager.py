"""S2 JobManager：多 job 并行调度（进程隔离）。

- submit(spec)      写 task.yaml + jobs 表（queued）→ 入队
- cancel(job_id)    SIGTERM worker / 标记 cancelled
- list()            列出所有 job
- reap()            轮询子进程、回收退出码、启动队列下一个
- run_forever()     manager 常驻循环（reap + stuck 检测 + 心跳）

并发模型（S2 红线，不许混用）：
  Job 间并行   = OS 进程（subprocess.Popen 起 worker）
  Job 内并行   = asyncio.to_thread（Engine 内部，不归 manager 管）

stuck 检测：worker 每 10s 更新 jobs.updated_at；manager 发现某 worker
超过 15min 未更新且进程还活着 → 标记 stuck 并 SIGTERM。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from secpt.core.job.models import JobRecord, TaskSpec
from secpt.core.job.resume import jobs_dir, write_task_snapshot
from secpt.core.job.store import JobStore

STUCK_AFTER_S = 15 * 60  # 15min 无心跳 → stuck
REAP_INTERVAL_S = 5.0
HEARTBEAT_GRACE_S = 30.0  # 启动头 30s 不算 stuck（worker 刚起来还没心跳）


class JobManager:
    def __init__(
        self,
        *,
        max_parallel_jobs: int = 4,
        db_path: str | Path = "var/secpt.sqlite",
        worker_python: str | None = None,
        worker_script: str | Path | None = None,
    ) -> None:
        """worker_script 可选：覆盖 worker 入口（单测用假 worker 脚本，
        生产保持 ``-m secpt.core.job.worker``）。"""
        self.max_parallel_jobs = max(1, max_parallel_jobs)
        self.store = JobStore(db_path)
        self.db_path = Path(db_path)
        # job_id -> (Popen, started_at)
        self._procs: dict[str, tuple[subprocess.Popen, float]] = {}
        self._queue: list[str] = []  # FIFO 排队中（jobs 表 status=queued）
        self._worker_python = worker_python or sys.executable
        self._worker_script = Path(worker_script) if worker_script else None

    # ---------------- 提交 ----------------

    def submit(self, spec: TaskSpec, run_config: dict | None = None) -> None:
        """提交任务：落盘 task.yaml + 写 jobs 表 queued + 入队。"""
        jdir = jobs_dir() / spec.job_id
        run_config = dict(run_config or {})
        run_config.setdefault("db_path", str(self.db_path))
        write_task_snapshot(jdir, spec, run_config)
        rec = self.store.get_job(spec.job_id)
        if rec is None:
            rec = JobRecord(job_id=spec.job_id, spec=spec, status="queued")
        else:
            rec.spec = spec
            rec.set_status("queued")
        self.store.upsert_job(rec)
        if spec.job_id not in self._queue and spec.job_id not in self._procs:
            self._queue.append(spec.job_id)

    # ---------------- 调度 ----------------

    def reap(self) -> None:
        """轮询子进程；回收退出码；启动队列中可跑的 job。"""
        # 1) 回收已退出的进程
        done = []
        for job_id, (proc, _t0) in self._procs.items():
            rc = proc.poll()
            if rc is not None:
                done.append(job_id)
                rec = self.store.get_job(job_id)
                if rec is not None and rec.status == "running" and rc != 0:
                    # 子进程自己会更新终态；这里只兜底（被 kill 等）
                    rec.set_status("failed", error=f"worker exited rc={rc}")
                    self.store.upsert_job(rec)
        for job_id in done:
            self._procs.pop(job_id, None)

        # 2) 从队列补位
        while len(self._procs) < self.max_parallel_jobs and self._queue:
            job_id = self._queue.pop(0)
            self._spawn(job_id)

    def _spawn(self, job_id: str) -> bool:
        """用 subprocess.Popen 起 worker 进程。"""
        jdir = jobs_dir() / job_id
        if not (jdir / "task.yaml").is_file():
            return False
        if self._worker_script is not None:
            # 单测入口：直接跑脚本（假 worker，不写终态，由测试自证）
            cmd = [self._worker_python, str(self._worker_script), "--job-id", job_id]
        else:
            cmd = [
                self._worker_python,
                "-m",
                "secpt.core.job.worker",
                "--job-id",
                job_id,
            ]
        env = os.environ.copy()
        # 让 worker 用同一个 db
        env.setdefault("SECPT_VAR", str(Path("var")))
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(".").resolve()),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._procs[job_id] = (proc, time.monotonic())
        rec = self.store.get_job(job_id)
        if rec is not None:
            rec.set_status("running")
            self.store.upsert_job(rec)
        return True

    # ---------------- 控制 ----------------

    def cancel(self, job_id: str) -> bool:
        """取消 job：在跑 → SIGTERM；排队 → 出队并标记 cancelled。

        跨进程场景（CLI cancel 新起进程）：本 manager 的 _procs 里没有该
        worker，则按 var/jobs/<job_id>/worker.pid 定位进程发 SIGTERM。
        """
        rec = self.store.get_job(job_id)
        if rec is None:
            return False
        if job_id in self._procs:
            proc, _ = self._procs[job_id]
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            rec.set_status("cancelled")
            self.store.upsert_job(rec)
            return True
        # 跨进程：worker 落盘的 pid 文件
        pid_file = jobs_dir() / job_id / "worker.pid"
        if pid_file.is_file():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                os.kill(pid, signal.SIGTERM)
                rec.set_status("cancelled")
                self.store.upsert_job(rec)
                return True
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        if job_id in self._queue:
            self._queue.remove(job_id)
        if rec.status in ("queued", "paused"):
            rec.set_status("cancelled")
            self.store.upsert_job(rec)
            return True
        return False

    def list(self) -> list[JobRecord]:
        return self.store.list_jobs()

    def get(self, job_id: str) -> JobRecord | None:
        return self.store.get_job(job_id)

    # ---------------- stuck 检测 ----------------

    def _check_stuck(self) -> None:
        """心跳超时检测：worker 15min 无更新且进程活着 → stuck + SIGTERM。"""
        now = time.monotonic()
        for job_id, (proc, t0) in list(self._procs.items()):
            if proc.poll() is not None:
                continue  # 已退出，reap 处理
            # 启动头 30s 不算
            if now - t0 < HEARTBEAT_GRACE_S:
                continue
            rec = self.store.get_job(job_id)
            if rec is None:
                continue
            try:
                updated = rec.updated_at
                from datetime import datetime

                dt = datetime.fromisoformat(updated)
                age = time.time() - dt.timestamp()
            except Exception:
                age = 0.0
            if age > STUCK_AFTER_S:
                print(f"[manager] job {job_id} stuck (no heartbeat {age:.0f}s), SIGTERM")
                rec.set_status("stuck")
                self.store.upsert_job(rec)
                with contextlib.suppress(ProcessLookupError):
                    proc.send_signal(signal.SIGTERM)

    # ---------------- 常驻循环 ----------------

    def run_forever(self, max_wait_s: float = 0.0) -> None:
        """reap + stuck 循环。max_wait_s>0 时跑该时长后返回（测试用）。"""
        t0 = time.monotonic()
        while True:
            self.reap()
            self._check_stuck()
            if max_wait_s > 0 and time.monotonic() - t0 >= max_wait_s:
                break
            # 全部结束后退出
            if not self._procs and not self._queue:
                if max_wait_s > 0:
                    break
                # 无任务时 sleep 短一些，等 submit
                time.sleep(REAP_INTERVAL_S)
                continue
            time.sleep(REAP_INTERVAL_S)


__all__ = ["JobManager"]
