"""archive_runtime 归档压缩脚本单测。"""

from __future__ import annotations

import gzip
import tarfile
import time
from pathlib import Path

from scripts.archive_runtime import (
    archive_job,
    archive_log,
    is_stale,
    redact_text,
    run,
    scan_job_for_secrets,
)


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


# ---------------- secrets 扫描与 redact ----------------


def test_redact_text_replaces_and_keeps_key_name() -> None:
    text = 'api_key=sk-abcdef1234567890abcdef and password="supersecret12345" remain'
    out, n = redact_text(text)
    assert n >= 2
    assert "sk-abcdef1234567890abcdef" not in out
    assert "supersecret12345" not in out
    assert "api_key=" in out and "password=" in out  # 键名保留，便于事后定位
    assert "remain" in out  # 普通内容不受影响


def test_scan_job_detects_secrets_without_value_leak(tmp_path: Path) -> None:
    d = tmp_path / "job-x"
    d.mkdir()
    (d / "events.jsonl").write_text(
        '{"payload": {"env": "DEEPSEEK_API_KEY=sk-abcdef1234567890abcdef"}}\n',
        encoding="utf-8",
    )
    hits = scan_job_for_secrets(d)
    assert hits and hits[0][0].name == "events.jsonl"
    assert all(t in {"openai_key", "keyval"} for _, t in hits)  # 只报类型不回显值


def test_scan_job_clean_text_no_false_positive(tmp_path: Path) -> None:
    d = tmp_path / "job-y"
    d.mkdir()
    (d / "report.md").write_text(
        "# 报告\n发现 flag{test_flag} 于 /etc/passwd，端口 8080 开放。\n",
        encoding="utf-8",
    )
    (d / "exploit.py").write_text(
        "import requests\nrequests.post('http://127.0.0.1:8000/api', json={'a': 1})\n",
        encoding="utf-8",
    )
    assert scan_job_for_secrets(d) == []


def test_archive_job_blocked_on_secrets(tmp_path: Path) -> None:
    """含密钥的 job：跳过归档（fail-closed），源目录与归档包均不存在。"""
    jobs_dir = tmp_path / "jobs"
    d = _make_job(jobs_dir, "job-secret")
    (d / "events.jsonl").write_text("key=sk-abcdef1234567890abcdef\n", encoding="utf-8")
    import os

    old = time.time() - 10 * 86400
    os.utime(d / "events.jsonl", (old, old))
    archive_dir = tmp_path / "archive" / "jobs"

    dst = archive_job(d, archive_dir)

    assert dst is None
    assert d.exists()  # 源目录保留待人工处理
    assert not list(archive_dir.glob("*.tar.gz"))


def test_archive_log_redacts_before_compress(tmp_path: Path) -> None:
    log = tmp_path / "run-2.log"
    log.write_text(
        "echo DEEPSEEK_API_KEY=sk-abcdef1234567890abcdef\nclean line\n", encoding="utf-8"
    )

    dst = archive_log(log)

    assert dst is not None
    with gzip.open(dst, "rt") as fh:
        content = fh.read()
    assert "sk-abcdef1234567890abcdef" not in content  # 敏感值不落档
    assert "clean line" in content
    assert not log.exists()
