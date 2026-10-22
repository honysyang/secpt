"""Sustainment 层：运行时产物生命周期管理（压缩/归档/整顿）。

Web 为主要任务入口后，var/jobs/ 只增不减：事件流、日志、SQLite wal 长期
积累。这里在 engagement 结束时做确定性压缩（`SECPT_COMPRESS=0` 关闭）：

- events.jsonl 轮转：保留尾部 keep_events 行在线，更早的 gzip 进 archive/
  **且保持 seq 单调**——轮转后在线文件首行是"归档位标"事件（seq=最后归档
  seq），之后 seq=行数+1 的分配规则自动续上行号，游标增量消费不感知轮转
- archive/watermark.json 记录 min_kept_seq，events_payload 据此回读归档
- frontier/hypothesis SQLite VACUUM（清 wal/碎片）
- 大日志文件（>256KB 的 *.log）gzip 打包
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import time


def compress_job_dir(base: str, *, keep_events: int = 500) -> dict:
    """压缩一个任务目录；返回 {archived_events, vacuumed, logs_packed}。"""
    from secpt.core.job.resume import jobs_dir

    jdir = jobs_dir() / base
    out = {"archived_events": 0, "vacuumed": 0, "logs_packed": 0}
    if not jdir.is_dir():
        return out
    archive = jdir / "archive"
    archive.mkdir(exist_ok=True)

    # 1) events.jsonl 轮转（保 seq 单调：位标事件承接归档尾部 seq）
    ev_path = jdir / "events.jsonl"
    if ev_path.is_file():
        try:
            lines = ev_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        if len(lines) > keep_events:
            kept = lines[-keep_events:]
            dropped = lines[:-keep_events]
            last_arch = 0
            for ln in reversed(dropped):
                with _suppress():
                    last_arch = max(last_arch, int(json.loads(ln).get("seq") or 0))
            gz_name = f"events-{int(time.time())}.jsonl.gz"
            with gzip.open(archive / gz_name, "wt", encoding="utf-8") as gz:
                gz.write("\n".join(dropped) + "\n")
            # 位标事件：seq=最后归档 seq → 在线文件行数承接，后续 seq 自动连续
            marker = {
                "seq": last_arch,
                "job_id": base,
                "ts": time.time(),
                "round": 0,
                "type": "loop_phase",
                "payload": {
                    "phase": "archive",
                    "reason": f"更早 {len(dropped)} 条事件已归档 {gz_name}（seq 单调位标）",
                },
            }
            tmp = jdir / ".events.jsonl.tmp"
            tmp.write_text(
                json.dumps(marker, ensure_ascii=False) + "\n" + "\n".join(kept) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, ev_path)
            first_kept = 0
            with _suppress():
                first_kept = int(json.loads(kept[0]).get("seq") or 0)
            (archive / "watermark.json").write_text(
                json.dumps(
                    {
                        "min_kept_seq": first_kept,
                        "archives": [gz_name],
                        "archived_upto": last_arch,
                        "ts": time.time(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            out["archived_events"] = len(dropped)

    # 2) SQLite VACUUM（frontier / hypothesis）
    for db in ("frontier.sqlite", "hypothesis.sqlite"):
        p = jdir / db
        if p.is_file():
            try:
                conn = sqlite3.connect(str(p), timeout=15)
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                finally:
                    conn.close()
                out["vacuumed"] += 1
            except sqlite3.Error:
                pass

    # 3) 大日志 gzip（*.log > 256KB；幂等：已有 .gz 跳过）
    for f in jdir.rglob("*.log"):
        try:
            if f.stat().st_size > 256 * 1024 and not (f.parent / (f.name + ".gz")).exists():
                with open(f, "rb") as src, gzip.open(str(f) + ".gz", "wb") as dst:
                    dst.writelines(src)
                f.unlink()
                out["logs_packed"] += 1
        except OSError:
            continue
    return out


def read_archived_events(base: str, after: int, upto_seq: int) -> list[dict]:
    """读回归档中 seq ∈ (after, upto_seq) 的事件（events_payload 轮转回读）。"""
    from secpt.core.job.resume import jobs_dir

    archive = jobs_dir() / base / "archive"
    wm_path = archive / "watermark.json"
    if not wm_path.is_file():
        return []
    try:
        wm = json.loads(wm_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for name in wm.get("archives", []):
        try:
            with gzip.open(archive / name, "rt", encoding="utf-8", errors="replace") as gz:
                for ln in gz:
                    with _suppress():
                        row = json.loads(ln)
                    if not isinstance(row, dict):
                        continue
                    seq = int(row.get("seq") or 0)
                    if after < seq < upto_seq:
                        out.append(row)
        except OSError:
            continue
    return out


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
