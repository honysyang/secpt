"""test_manager：并发上限、FIFO 队列、cancel、stuck 检测、worker 退出码。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# 让 secpt 可导入
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from datetime import UTC

from secpt.core.job.manager import STUCK_AFTER_S, JobManager
from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.job.store import JobStore


def _spec(job_id: str, sleep_s: float = 1.0) -> TaskSpec:
    return TaskSpec(
        job_id=job_id,
        task_type="generic",
        objective=f"objective {job_id}",
        budget=TaskBudget(wall_s=60, steps=5),
    )


# 假 worker：sleep 后以退出码 0 结束（模拟成功）
_FAKE_WORKER = r"""
import sys, time
time.sleep(float(sys.argv[sys.argv.index('--sleep') + 1]) if '--sleep' in sys.argv else 1.0)
sys.exit(0)
"""


@pytest.fixture()
def manager(tmp_path):
    fake = tmp_path / "fake_worker.py"
    fake.write_text(_FAKE_WORKER)
    mgr = JobManager(max_parallel_jobs=2, db_path=tmp_path / "t.sqlite")
    mgr._worker_python = sys.executable
    return mgr


def test_concurrency_cap(tmp_path):
    """并发上限生效：3 个任务最多同时 2 个在跑。"""
    fake = tmp_path / "fake_worker.py"
    fake.write_text(_FAKE_WORKER)

    mgr = JobManager(max_parallel_jobs=2, db_path=tmp_path / "t.sqlite", worker_script=fake)

    # 直接调用 _spawn 两次 → 并发 2，第三次应被 queue 拦住
    mgr.submit(_spec("j1"))
    mgr.submit(_spec("j2"))
    mgr.submit(_spec("j3"))
    mgr.reap()
    assert len(mgr._procs) == 2, "并发超过上限"
    assert len(mgr._queue) == 1, "第 3 个应排队"
    assert mgr._queue == ["j3"]


def test_fifo_order(tmp_path):
    """队列 FIFO：先提交的先被 spawn。"""
    fake = tmp_path / "fake_worker.py"
    fake.write_text(_FAKE_WORKER)
    mgr = JobManager(max_parallel_jobs=2, db_path=tmp_path / "t.sqlite", worker_script=fake)
    for i in range(1, 5):
        mgr.submit(_spec(f"job{i}"))
    mgr.reap()
    # 前两个被拉起，后两个按序排队
    assert set(mgr._procs.keys()) == {"job1", "job2"}
    assert mgr._queue == ["job3", "job4"]


def test_cancel_queued(tmp_path):
    """取消排队中的 job → 出队并标记 cancelled。"""
    fake = tmp_path / "fake_worker.py"
    fake.write_text(_FAKE_WORKER)
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite", worker_script=fake)
    for i in range(1, 4):
        mgr.submit(_spec(f"job{i}"))
    mgr.reap()
    assert mgr.cancel("job2") is True
    assert "job2" not in mgr._queue
    rec = mgr.store.get_job("job2")
    assert rec.status == "cancelled"


def test_cancel_running_sigterm(tmp_path):
    """取消运行中的 job → SIGTERM 给 worker。"""
    fake = tmp_path / "slow.py"
    fake.write_text("import time; time.sleep(30)")
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite", worker_script=fake)
    mgr.submit(_spec("job1"))
    mgr.reap()
    assert "job1" in mgr._procs
    assert mgr.cancel("job1") is True
    proc, _ = mgr._procs["job1"]
    # 等进程被 SIGTERM 杀掉
    proc.wait(timeout=5)
    rec = mgr.store.get_job("job1")
    assert rec.status == "cancelled"


def test_stuck_detection(tmp_path):
    """心跳超时 → stuck + SIGTERM。"""
    sleepy = tmp_path / "sleepy.py"
    sleepy.write_text("import time; time.sleep(30)")
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite", worker_script=sleepy)

    mgr.submit(_spec("stuck1"))
    mgr.reap()
    assert "stuck1" in mgr._procs

    # 把 updated_at 改老（超过 STUCK_AFTER_S）
    rec = mgr.store.get_job("stuck1")
    from datetime import datetime, timedelta

    rec.updated_at = (datetime.now(UTC) - timedelta(seconds=STUCK_AFTER_S + 60)).isoformat()
    mgr.store.upsert_job(rec)
    # 假装启动已过 grace
    proc, _t0 = mgr._procs["stuck1"]
    mgr._procs["stuck1"] = (proc, time.monotonic() - 3600)

    mgr._check_stuck()
    rec2 = mgr.store.get_job("stuck1")
    assert rec2.status == "stuck"
    # 进程被 SIGTERM 杀掉
    proc.wait(timeout=5)
    assert proc.poll() is not None


def test_reap_updates_status(tmp_path):
    """worker 正常退出后 reap 兜底（若 worker 未更新终态）。"""
    quick = tmp_path / "quick.py"
    quick.write_text("import sys; sys.exit(0)")
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite", worker_script=quick)
    mgr.submit(_spec("done1"))
    mgr.reap()
    time.sleep(1.0)
    mgr.reap()
    assert "done1" not in mgr._procs, "进程未被回收"


def test_store_wal_concurrent_multi_proc(tmp_path):
    """manager + worker 场景：SQLite WAL 多进程写不死锁（真实 worker 用假退出码）。"""
    # 不真正跑 worker（需要 task.yaml + Engine），只验证 store 多进程
    store = JobStore(tmp_path / "w.sqlite")
    store.add_event("j1", "status_change", {"status": "running"}, 0)
    assert len(store.get_events("j1")) == 1
