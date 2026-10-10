"""test_store：WAL 下并发写不死锁；jobs/events/findings 基本存取。"""

from __future__ import annotations

import threading

import pytest

from secpt.core.job.models import TaskSpec
from secpt.core.job.store import JobStore


def _make_record(store: JobStore, job_id: str):
    spec = TaskSpec(
        job_id=job_id,
        task_type="ctf",
        objective=f"objective {job_id}",
        allow_hosts=["10.0.0.1"],
    )
    from secpt.core.job.models import JobRecord

    return JobRecord(job_id=job_id, spec=spec, status="queued")


def test_upsert_get(tmp_path):
    store = JobStore(tmp_path / "t.sqlite")
    rec = _make_record(store, "j1")
    store.upsert_job(rec)
    got = store.get_job("j1")
    assert got is not None
    assert got.spec.objective == "objective j1"
    assert got.status == "queued"


def test_update_status(tmp_path):
    store = JobStore(tmp_path / "t.sqlite")
    rec = _make_record(store, "j2")
    store.upsert_job(rec)
    rec.status = "running"
    store.upsert_job(rec)
    got = store.get_job("j2")
    assert got.status == "running"


def test_invalid_status_rejected(tmp_path):
    store = JobStore(tmp_path / "t.sqlite")
    rec = _make_record(store, "j3")
    rec.status = "nonsense"
    with pytest.raises(ValueError):
        store.upsert_job(rec)


def test_list_and_delete(tmp_path):
    store = JobStore(tmp_path / "t.sqlite")
    for i in range(3):
        store.upsert_job(_make_record(store, f"j{i}"))
    assert len(store.list_jobs()) == 3
    assert len(store.list_jobs(status="queued")) == 3
    store.delete_job("j1")
    assert store.get_job("j1") is None
    assert len(store.list_jobs()) == 2


def test_wal_concurrent_write_no_deadlock(tmp_path):
    """多线程并发写（WAL + busy_timeout）不死锁、不丢数据。"""
    store = JobStore(tmp_path / "t.sqlite")
    n_threads = 8
    n_each = 25
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            for i in range(n_each):
                store.add_event(
                    f"job-{idx}", "tool_result", {"tool": f"t{i}", "status": "ok", "bytes": i}, 0
                )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent write errors: {errors}"
    total = sum(len(store.get_events(f"job-{i}", limit=10000)) for i in range(n_threads))
    assert total == n_threads * n_each
