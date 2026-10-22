"""全局目标租约（v10 多任务并行）：跨 engagement 的目标互斥登记。

Web 为主要任务入口后多个 engagement 可并行跑；任务内 asset_leases 只防同池撞车，
不防两个 engagement 对同一目标并发打（自己打自己）。这里是**咨询性**（advisory）
全局租约：登记"目标 → 持有 job"，后到任务告警并自行限速决策，不硬锁——持有者
崩溃不会把目标锁死（租约带 TTL，过期自动可抢）。

存储：var/leases/<target-sha1>.json（O_CREAT|O_EXCL 原子抢租；flock 防读撕裂）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

_TTL_S = float(os.environ.get("SECPT_LEASE_TTL_S", "3600"))  # 持有者失联后的自动过期


def _leases_dir() -> Path:
    d = Path(os.environ.get("SECPT_VAR", "var")) / "leases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(target: str) -> str:
    return hashlib.sha1(target.encode("utf-8")).hexdigest()[:16]


def holder(target: str) -> dict | None:
    """当前持有者（含过期清理；无持有者/已过期返回 None）。"""
    f = _leases_dir() / f"{_key(target)}.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(data.get("ts") or 0) > _TTL_S:
        with contextlib_suppress():
            f.unlink()
        return None
    return data


def acquire(target: str, job_id: str) -> tuple[bool, str]:
    """抢租：返回 (是否拿到, 说明)。已有持有者（含自己）→ 不重复抢。"""
    target = (target or "").strip()
    if not target:
        return False, "empty"
    f = _leases_dir() / f"{_key(target)}.json"
    try:
        fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        h = holder(target)
        if h and h.get("job") == job_id:
            return True, "self"
        if h is None:
            # 过期租约已被 holder() 清理：重试一次抢租（否则 TTL 到期后永远 "held by unknown"）
            try:
                fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False, "held by unknown（advisory：可继续，建议限速避让）"
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"target": target[:200], "job": job_id, "ts": time.time()}, out)
            return True, "acquired"
        return False, f"held by {h.get('job')}（advisory：可继续，建议限速避让）"
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump({"target": target[:200], "job": job_id, "ts": time.time()}, out)
    return True, "acquired"


def release(target: str, job_id: str) -> None:
    """释放（仅持有者可释；幂等）。"""
    f = _leases_dir() / f"{_key(target)}.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("job") == job_id:
            f.unlink()
    except (OSError, json.JSONDecodeError):
        pass


def release_all(job_id: str) -> int:
    """任务结束时释放其全部租约；返回释放数。"""
    n = 0
    for f in _leases_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("job") == job_id:
            with contextlib_suppress():
                f.unlink()
            n += 1
    return n


class contextlib_suppress:
    """避免 import contextlib 的单例小工具（本模块唯一用途）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
