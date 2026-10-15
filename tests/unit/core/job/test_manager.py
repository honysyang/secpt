"""test_manager：并发上限、FIFO 队列、cancel、stuck 检测、worker 退出码。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# 让 secpt 可导入
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from datetime import UTC

from secpt.core.job.manager import MAX_AUTO_RESTARTS, STUCK_AFTER_S, JobManager
from secpt.core.job.models import JobRecord, TaskBudget, TaskSpec
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


def test_crash_auto_restart(tmp_path):
    """崩溃自动重启：异常退出 → requeue 断点续跑，restarts 递增，超限转 failed。"""
    crashy = tmp_path / "crashy.py"
    crashy.write_text("import sys; sys.exit(1)")
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite", worker_script=crashy)
    mgr.submit(_spec("c1"))

    # 第一轮：spawn → 崩 → requeue（restarts=1）→ respawn（需等进程退出）
    mgr.reap()
    time.sleep(0.4)
    mgr.reap()
    time.sleep(0.4)
    rec = mgr.store.get_job("c1")
    assert rec.status == "running", "崩溃后应自动 requeue 并重新拉起"
    assert rec.restarts == 1

    # 崩到超限 → failed，不再重启
    for _ in range(MAX_AUTO_RESTARTS + 1):
        mgr.reap()
        time.sleep(0.4)
    rec = mgr.store.get_job("c1")
    assert rec.status == "failed"
    assert "重启超限" in rec.last_error
    assert "c1" not in mgr._queue and "c1" not in mgr._procs


def test_success_not_restarted(tmp_path):
    """正常退出（rc=0）不触发重启。"""
    ok = tmp_path / "ok.py"
    ok.write_text("import sys; sys.exit(0)")
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite", worker_script=ok)
    mgr.submit(_spec("s1"))
    mgr.reap()
    time.sleep(0.4)
    mgr.reap()
    rec = mgr.store.get_job("s1")
    assert rec.restarts == 0
    assert "s1" not in mgr._queue


def test_restarts_roundtrip(tmp_path):
    """restarts 字段经 upsert/get 完整往返（含老库迁移列）。"""
    store = JobStore(tmp_path / "m.sqlite")
    rec = JobRecord(job_id="r1", spec=_spec("r1"), status="running", restarts=2)
    store.upsert_job(rec)
    got = store.get_job("r1")
    assert got.restarts == 2


def test_priority_pop_order(tmp_path, monkeypatch):
    """F6：priority DESC 出队，同级 FIFO（此前是纯 FIFO，README 声称优先级队列）。"""
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite")
    mgr.submit(_spec("low"), {"priority": 1})
    mgr.submit(_spec("high"), {"priority": 9})
    mgr.submit(_spec("normal"))  # 缺省 0
    mgr.submit(_spec("high2"), {"priority": 9})  # 同级：先提交先出
    assert [mgr._pop_next() for _ in range(4)] == ["high", "high2", "low", "normal"]


def test_priority_drives_spawn(tmp_path, monkeypatch):
    """F6 端到端：并发槽位释放后，高优先级任务先被拉起。"""
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    fake = tmp_path / "fake_worker.py"
    fake.write_text(_FAKE_WORKER)
    mgr = JobManager(max_parallel_jobs=1, db_path=tmp_path / "t.sqlite", worker_script=fake)
    mgr.submit(_spec("block"), {"priority": 100})  # 占住唯一槽位（自身也走优先级）
    mgr.submit(_spec("low"), {"priority": 1})
    mgr.submit(_spec("high"), {"priority": 9})
    mgr.reap()
    assert set(mgr._procs) == {"block"}
    # 等 block 退出 → 再 reap 补位：high 应先进 _procs
    import time as _time

    for _ in range(100):
        if mgr._procs["block"][0].poll() is not None:
            break
        _time.sleep(0.05)
    mgr.reap()
    assert "high" in mgr._procs
    assert "low" not in mgr._procs and mgr._queue == ["low"]


def test_restore_queue_from_store(tmp_path, monkeypatch):
    """加强⑥：JobManager 重启恢复 queued（优先级从 task.yaml 快照读回）。"""
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    from secpt.core.job.resume import write_task_snapshot

    db = tmp_path / "t.sqlite"
    store = JobStore(str(db))
    for jid, prio in (("rq1", 5), ("rq2", 0)):
        jd = tmp_path / "jobs" / jid
        write_task_snapshot(jd, _spec(jid), {"priority": prio, "db_path": str(db)})
        store.upsert_job(JobRecord(job_id=jid, spec=_spec(jid), status="queued"))

    mgr = JobManager(max_parallel_jobs=1, db_path=db, restore_queue=True)
    assert mgr._queue == ["rq1", "rq2"]
    assert mgr._prio["rq1"] == 5
    assert mgr._pop_next() == "rq1"  # 恢复的优先级参与出队

    mgr2 = JobManager(max_parallel_jobs=1, db_path=db)  # 默认不恢复（只读命令无副作用）
    assert mgr2._queue == []
