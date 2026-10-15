#!/usr/bin/env python3
"""运行时数据归档压缩：var/jobs 终态任务 + 根/logs 散落运行日志。

背景：每次运行产生 events.jsonl / rounds/ 快照 / workspace 产物（单 job 可达数百 MB），
logs/ 与 var/jobs 只增不减。本脚本把「超过 N 天没有修改」的 job 目录整包 tar.gz 归档
（活跃 job 的目录 mtime 持续更新，天然不会被误归档），散落 .log 单文件 gzip。

安全设计：
- 默认 dry-run（只打印将归档清单），--apply 才真正落盘删除
- tar.gz 原子替换：先写临时包，校验可读后（zipfile.testzip 等价 tarfile getmembers）
  才删除源目录；失败保留源不动
- resume 依赖的 events.jsonl/rounds 只会被归档终态 job——活跃 job 不触碰

用法：
    python scripts/archive_runtime.py                     # dry-run 预览（jobs 7 天 + logs 7 天）
    python scripts/archive_runtime.py --apply             # 实际归档
    python scripts/archive_runtime.py --days 3 --apply    # 更激进的阈值
    python scripts/archive_runtime.py --purge-days 30 --apply  # 连 30 天前的归档包一起删
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import tarfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def is_stale(path: Path, days: float) -> bool:
    """mtime 距今超过 days 天即视为终态/废弃。"""
    age_s = time.time() - path.stat().st_mtime
    return age_s > days * 86400


def archive_job(job_dir: Path, archive_dir: Path) -> Path | None:
    """把单个 job 目录打包为 var/archive/jobs/<job_id>.tar.gz（原子替换）。

    返回归档包路径；失败返回 None（源目录保持原样）。
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / f"{job_dir.name}.tar.gz"
    tmp = dst.with_suffix(".tar.gz.tmp")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(job_dir, arcname=job_dir.name)
        # 校验：包可完整读取才替换（防半截包）
        with tarfile.open(tmp, "r:gz") as tar:
            _ = tar.getmembers()
        tmp.replace(dst)  # 原子替换
    except (OSError, tarfile.TarError) as e:
        print(f"  [跳过] {job_dir.name}: 归档失败 {e}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return None
    shutil.rmtree(job_dir)
    return dst


def archive_log(log_file: Path) -> Path | None:
    """单个 .log → .gz（原子：成功后删源）。同名 .gz 已存在时跳过。"""
    dst = log_file.with_suffix(log_file.suffix + ".gz")
    if dst.exists():
        return None
    tmp = dst.with_suffix(".gz.tmp")
    try:
        with open(log_file, "rb") as src, gzip.open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
        tmp.replace(dst)
    except OSError as e:
        print(f"  [跳过] {log_file.name}: 压缩失败 {e}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return None
    log_file.unlink()
    return dst


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}TB"


def run(
    jobs_dir: Path, logs_dirs: list[Path], days: float, apply: bool, purge_days: float | None
) -> None:
    jobs_archive = jobs_dir.parent / "archive" / "jobs"

    # ---- 1. 归档终态 job 目录 ----
    jobs = (
        sorted(
            (d for d in jobs_dir.iterdir() if d.is_dir() and is_stale(d, days)),
            key=lambda d: d.stat().st_mtime,
        )
        if jobs_dir.exists()
        else []
    )
    jobs_bytes = sum(f.stat().st_size for d in jobs for f in d.rglob("*") if f.is_file())
    print(f"[jobs] 待归档 {len(jobs)} 个 job 目录（{human(jobs_bytes)}，mtime > {days} 天）")
    for d in jobs[:20]:
        print(f"    - {d.name}")
    if len(jobs) > 20:
        print(f"    ... 共 {len(jobs)} 个")
    if apply and jobs:
        ok = 0
        for d in jobs:
            if archive_job(d, jobs_archive):
                ok += 1
        print(f"[jobs] 已归档 {ok}/{len(jobs)} → {jobs_archive}")

    # ---- 2. gzip 散落日志 ----
    logs: list[Path] = []
    seen: set[Path] = set()
    for ld in logs_dirs:
        if not ld.exists():
            continue
        for f in sorted(ld.iterdir()):
            if f.suffix == ".log" and f.is_file() and is_stale(f, days) and f not in seen:
                seen.add(f)
                logs.append(f)
    logs_bytes = sum(f.stat().st_size for f in logs)
    print(f"[logs] 待压缩 {len(logs)} 个日志（{human(logs_bytes)}，mtime > {days} 天）")
    if apply and logs:
        ok = 0
        for f in logs:
            if archive_log(f):
                ok += 1
        print(f"[logs] 已压缩 {ok}/{len(logs)}")

    # ---- 3. 清理超期归档包 ----
    if purge_days is not None:
        pkgs = (
            [p for p in jobs_archive.glob("*.tar.gz") if p.exists() and is_stale(p, purge_days)]
            if jobs_archive.exists()
            else []
        )
        print(f"[purge] 超期归档包 {len(pkgs)} 个（mtime > {purge_days} 天）")
        if apply and pkgs:
            for p in pkgs:
                p.unlink()
            print(f"[purge] 已删除 {len(pkgs)} 个")

    if not apply:
        print("\n（dry-run 预览，未做任何改动；加 --apply 执行）")


def main() -> int:
    ap = argparse.ArgumentParser(description="var/jobs + 运行日志 归档压缩")
    ap.add_argument("--days", type=float, default=7.0, help="mtime 超过 N 天才归档（默认 7）")
    ap.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    ap.add_argument(
        "--purge-days", type=float, default=None, help="归档包超过 N 天直接删除（可选）"
    )
    ap.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="仓库根（测试注入用）")
    args = ap.parse_args()

    jobs_dir = args.repo_root / "var" / "jobs"
    logs_dirs = [args.repo_root / "logs", args.repo_root]  # logs/ 优先 + 根目录兜底
    run(jobs_dir, logs_dirs, args.days, args.apply, args.purge_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
