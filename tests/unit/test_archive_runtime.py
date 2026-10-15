"""archive_runtime 归档压缩脚本单测。"""

from __future__ import annotations

import gzip
import tarfile
import time
from pathlib import Path

from scripts.archive_runtime import archive_job, archive_log, is_stale, run


def _make_job(jobs_dir: Path, name: str, age_days: float = 10.0) -> Path:
    """造一个带 events.jsonl + ws 产物的 job 目录，mtime 回拨 age_days 天。"""
    d = jobs_dir / name
    (d / "ws").mkdir(parents=True)
    (d / "events.jsonl").write_text('{"seq": 1, "type": "step"}\n', encoding="utf-8")
    (d / "ws" / "out.txt").write_text("x" * 100, encoding="utf-8")
    old = time.time() - age_days * 86400
    for p in (d, d / "events.jsonl", d / "ws"):
        import os

        os.utime(p, (old, old))
    return d


def test_is_stale(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("x")
    assert not is_stale(p, 7)  # 刚创建
    old = time.time() - 8 * 86400
    p.touch()
    import os

    os.utime(p, (old, old))
    assert is_stale(p, 7)
    assert not is_stale(p, 9)


def test_archive_job_roundtrip(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    d = _make_job(jobs_dir, "job-a")
    archive_dir = tmp_path / "archive" / "jobs"

    dst = archive_job(d, archive_dir)

    assert dst is not None and dst.exists()
    assert not d.exists()  # 源目录已删除
    with tarfile.open(dst, "r:gz") as tar:  # 归档包完整可读
        names = tar.getnames()
    assert any("events.jsonl" in n for n in names)


def test_archive_log_roundtrip(tmp_path: Path) -> None:
    log = tmp_path / "run-1.log"
    log.write_text("line\n" * 1000, encoding="utf-8")

    dst = archive_log(log)

    assert dst is not None and dst.name == "run-1.log.gz"
    assert not log.exists()
    with gzip.open(dst, "rt") as fh:
        assert fh.read() == "line\n" * 1000
    # 同名 .gz 已存在 → 跳过（返回 None，不重复压）
    log.write_text("new", encoding="utf-8")
    assert archive_log(log) is None


def test_run_dry_run_touches_nothing(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    d = _make_job(jobs_dir, "job-b")
    log = tmp_path / "x.log"
    log.write_text("log")
    import os

    os.utime(log, (time.time() - 30 * 86400,) * 2)

    run(jobs_dir, [tmp_path], days=7, apply=False, purge_days=None)

    assert d.exists() and log.exists()  # dry-run 不动任何文件


def test_run_apply_archives_stale_only(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    stale = _make_job(jobs_dir, "job-old", age_days=10)
    fresh = _make_job(jobs_dir, "job-new", age_days=0.01)
    log = tmp_path / "y.log"
    log.write_text("log")
    import os

    os.utime(log, (time.time() - 30 * 86400,) * 2)

    run(jobs_dir, [tmp_path], days=7, apply=True, purge_days=None)

    assert not stale.exists()  # 过期 job 已归档删除
    assert (tmp_path / "archive" / "jobs" / "job-old.tar.gz").exists()
    assert fresh.exists()  # 活跃 job 不触碰
    assert not log.exists() and (tmp_path / "y.log.gz").exists()  # 日志已压缩
