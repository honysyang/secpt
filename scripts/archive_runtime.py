#!/usr/bin/env python3
"""运行时数据归档压缩：var/jobs 终态任务 + 根/logs 散落运行日志。

背景：每次运行产生 events.jsonl / rounds/ 快照 / workspace 产物（单 job 可达数百 MB），
logs/ 与 var/jobs 只增不减。本脚本把「超过 N 天没有修改」的 job 目录整包 tar.gz 归档
（活跃 job 的目录 mtime 持续更新，天然不会被误归档），散落 .log 单文件 gzip。

安全设计：
- 默认 dry-run（只打印将归档清单），--apply 才真正落盘删除
- tar.gz 原子替换：先写临时包，校验可读后（getmembers 全量校验）才删除源目录；
  失败保留源不动
- resume 依赖的 events.jsonl/rounds 只会被归档终态 job——活跃 job 不触碰
- secrets 扫描（防敏感数据永久入档）：
  * job 目录：文本类文件逐行扫描密钥模式，命中即「跳过该 job」（fail-closed），
    只打印文件与类型不打印敏感值，待人工确认/清理后重跑
  * 散落 .log：运行日志非证据链，压缩前原位 redact（命中段替换为 ***REDACTED:<type>***）

用法：
    python scripts/archive_runtime.py                     # dry-run 预览（jobs 7 天 + logs 7 天）
    python scripts/archive_runtime.py --apply             # 实际归档
    python scripts/archive_runtime.py --days 3 --apply    # 更激进的阈值
    python scripts/archive_runtime.py --purge-days 30 --apply  # 连 30 天前的归档包一起删
"""

from __future__ import annotations

import argparse
import gzip
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------- secrets 扫描 ----------------
# (类型名, 正则, redact 替换模板)；\g<1> 保留键名便于事后定位上下文
SECRETS_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}"), "***REDACTED:key***"),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), "***REDACTED:key***"),
    (
        "bearer",
        re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._-]{16,}"),
        r"\g<1>***REDACTED***",
    ),
    (
        "keyval",
        re.compile(
            r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[=:]\s*[\"']?"
            r"[A-Za-z0-9/+=._-]{16,}"
        ),
        r"\g<1>=***REDACTED***",
    ),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "***REDACTED***"),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
        "***REDACTED:jwt***",
    ),
]

# job 目录内参与扫描的文本扩展名（二进制/归档类不扫，避免误报与浪费）
TEXT_SUFFIXES = {
    ".jsonl",
    ".json",
    ".log",
    ".txt",
    ".md",
    ".py",
    ".sh",
    ".yaml",
    ".yml",
    ".conf",
    ".ini",
    ".csv",
    ".xml",
    ".html",
    ".js",
}
MAX_TYPES_PER_FILE = 3  # 每文件最多记录的命中类型数（控制告警噪音）


def redact_text(text: str) -> tuple[str, int]:
    """替换文本中的疑似敏感段。返回（替换后文本, 命中次数）。"""
    total = 0
    for _name, pat, repl in SECRETS_PATTERNS:
        text, n = pat.subn(repl, text)
        total += n
    return text, total


def scan_job_for_secrets(job_dir: Path) -> list[tuple[Path, str]]:
    """扫描 job 目录内文本文件，返回 [(文件, 命中类型), ...]。

    每文件最多记录 MAX_TYPES_PER_FILE 类命中；只报告类型与文件，不回显敏感值。
    """
    hits: list[tuple[Path, str]] = []
    for f in sorted(job_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        types: set[str] = set()
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    for name, pat, _repl in SECRETS_PATTERNS:
                        if name not in types and pat.search(line):
                            types.add(name)
                    if len(types) >= MAX_TYPES_PER_FILE:
                        break
        except OSError:
            continue
        hits.extend((f, t) for t in sorted(types))
    return hits


def is_stale(path: Path, days: float) -> bool:
    """mtime 距今超过 days 天即视为终态/废弃。"""
    age_s = time.time() - path.stat().st_mtime
    return age_s > days * 86400


def archive_job(job_dir: Path, archive_dir: Path) -> Path | None:
    """把单个 job 目录打包为 var/archive/jobs/<job_id>.tar.gz（原子替换）。

    归档前做 secrets 扫描：命中即跳过（fail-closed，源目录保持原样）。
    返回归档包路径；失败/含敏感数据返回 None。
    """
    hits = scan_job_for_secrets(job_dir)
    if hits:
        print(
            f"  [拦截] {job_dir.name}: 检出疑似敏感数据，已跳过归档（源目录保留）：",
            file=sys.stderr,
        )
        for f, t in hits[:10]:
            rel = f.relative_to(job_dir)
            print(f"      - {rel}  [{t}]", file=sys.stderr)
        if len(hits) > 10:
            print(f"      ... 共 {len(hits)} 处", file=sys.stderr)
        return None

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
    """单个 .log → .gz，压缩前 redact 敏感段（运行日志非证据链，可安全改写）。

    同名 .gz 已存在时跳过。
    """
    dst = log_file.with_suffix(log_file.suffix + ".gz")
    if dst.exists():
        return None
    tmp = dst.with_suffix(".gz.tmp")
    try:
        n_hits = 0
        with (
            open(log_file, encoding="utf-8", errors="ignore") as src,
            gzip.open(tmp, "wt", encoding="utf-8", errors="replace") as out,
        ):
            for line in src:
                line, n = redact_text(line)
                n_hits += n
                out.write(line)
        tmp.replace(dst)
        if n_hits:
            print(f"  [redact] {log_file.name}: 替换 {n_hits} 处疑似敏感段", file=sys.stderr)
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

    # ---- 2. gzip 散落日志（压缩前 redact） ----
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
    ap = argparse.ArgumentParser(description="var/jobs + 运行日志 归档压缩（含 secrets 扫描）")
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
