"""S4：证据盒 / 执行器 / 指纹（BUILD_SPEC §7.2、§6.5；S4 追加验收四条）。"""

from __future__ import annotations

import hashlib
import json
import time

from pa.infra import evidence, executor
from pa.infra.executor import MAX_LINE_BYTES
from pa.infra.fp import VOLATILE_KEYS, fingerprint


def _raw(content: dict) -> bytes:
    return json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")


def test_evidence_put_idempotent_and_verified(tmp_mission):
    content = {"tool": "probe", "stdout": "open"}
    raw = _raw(content)
    eid1 = evidence.put(tmp_mission, raw, task_id="t1")
    eid2 = evidence.put(tmp_mission, raw, task_id="t1")
    assert eid1 == eid2 == "ev-" + hashlib.sha256(raw).hexdigest()[:16]
    assert evidence.verify(tmp_mission, eid1) is True
    sha = hashlib.sha256(raw).hexdigest()
    d = tmp_mission / "evidence" / sha[:2]
    assert sorted(p.name for p in d.iterdir()) == [f"{sha}.json", f"{sha}.meta.json"]
    meta = json.loads((d / f"{sha}.meta.json").read_text(encoding="utf-8"))
    assert meta["task_id"] == "t1" and meta["sha"] == sha and "ts" in meta


def test_evidence_torn_write_self_heals(tmp_mission):
    """追加验收一：模拟写到一半中断（内容截断 + 残留 tmp），重放同内容自愈。"""
    content = {"tool": "shell", "stdout": "A" * 1000}
    raw = _raw(content)
    sha = hashlib.sha256(raw).hexdigest()
    d = tmp_mission / "evidence" / sha[:2]
    d.mkdir(parents=True)
    (d / f"{sha}.json").write_bytes(raw[: len(raw) // 2])  # 截断的半份
    (d / f"{sha}.json.999.tmp").write_bytes(b"garbage")  # 中断残留
    eid = "ev-" + sha[:16]
    assert evidence.verify(tmp_mission, eid) is False  # 损坏可诊断
    assert evidence.put(tmp_mission, raw, task_id="t1") == eid  # 同 id，自愈
    assert evidence.verify(tmp_mission, eid) is True
    # 中断残留的 tmp 与本次原子写的 tmp 命名同族，一律不参与寻址（被忽略），
    # 不需要清理它来证明一致性
    assert (d / f"{sha}.json").read_bytes() == raw
    assert evidence.put(tmp_mission, raw, task_id="t1") == eid  # 再 put 不重写


def test_executor_captures_stdout_and_rc(tmp_mission):
    r = executor.run(["python3", "-c", "print('hi')"], timeout=10, workdir=tmp_mission)
    assert r.exit_code == 0 and r.stdout == b"hi\n"
    assert r.stderr == b"" and not r.timed_out and not r.truncated
    r2 = executor.run(
        ["python3", "-c", "import sys; sys.exit(3)"], timeout=10, workdir=tmp_mission
    )
    assert r2.exit_code == 3


def _pid_alive(pid: int) -> bool:
    """zombie（已被杀、等待回收）与存活都算'还在'，需区分。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            state = f.read().rsplit(b")", 1)[1].split()[0]
        return state not in (b"Z", b"X")
    except OSError:
        return False


def test_executor_timeout_kills_process_group(tmp_mission):
    """追加验收二：超时后整个进程组退出，后台子进程不残留。"""
    marker = tmp_mission / "child.pid"
    r = executor.run(
        ["bash", "-c", f"sleep 300 & echo $! > {marker}; sleep 300"],
        timeout=1,
        workdir=tmp_mission,
    )
    assert r.timed_out is True and r.exit_code == -9
    child = int(marker.read_text().strip())
    for _ in range(100):  # 给 init 回收 zombie 的时间窗
        if not _pid_alive(child):
            break
        time.sleep(0.05)
    assert _pid_alive(child) is False, f"子进程 {child} 残留"


def test_executor_truncates_long_stdout_line(tmp_mission):
    """追加验收三：单行超 200KB 截断，truncated=True 且记录 original_bytes。"""
    r = executor.run(
        ["python3", "-c", "import sys; sys.stdout.write('x' * 500000 + '\\n')"],
        timeout=30,
        workdir=tmp_mission,
    )
    assert r.truncated is True
    assert r.original_bytes == 500001
    assert len(r.stdout) == MAX_LINE_BYTES + 1  # 截断 + 换行
    assert r.stdout == b"x" * MAX_LINE_BYTES + b"\n"
    eid = evidence.put(
        tmp_mission,
        r.stdout,
        task_id="t1",
        extra={
            "truncated": r.truncated,
            "original_bytes": r.original_bytes,
            "truncated_at": r.truncated_at,
        },
    )
    assert evidence.verify(tmp_mission, eid) is True


def test_executor_truncates_total_at_2mb(tmp_mission):
    """裁定 4：单条证据总量 2MB 上限，truncated_at="total"。"""
    r = executor.run(
        ["python3", "-c", "import sys; sys.stdout.write(('x' * 100 + '\\n') * 25000)"],
        timeout=30,
        workdir=tmp_mission,
    )
    assert r.original_bytes == 101 * 25000
    assert r.truncated is True and r.truncated_at == "total"
    assert len(r.stdout) == executor.MAX_TOTAL_BYTES
    assert r.stdout.count(b"\n") == executor.MAX_TOTAL_BYTES // 101  # 行结构完整


def test_fingerprint_ignores_volatile_keys(tmp_mission):
    """追加验收四：timeout/retries/delay/jitter 不参与指纹。"""
    assert VOLATILE_KEYS == frozenset({"timeout", "retries", "delay", "jitter"})
    base = {"target": "10.0.0.1", "port": 443, "timeout": 60}
    same = {
        "target": "10.0.0.1",
        "port": 443,
        "timeout": 120,
        "retries": 3,
        "delay": 1,
        "jitter": 0.5,
    }
    assert fingerprint("probe", base) == fingerprint("probe", same)
    assert fingerprint("probe", base) != fingerprint("probe", {**base, "port": 80})
    assert fingerprint("probe", base) != fingerprint(
        "probe", {**base, "target": "10.0.0.2"}
    )
    assert len(fingerprint("probe", base)) == 32  # §6.5 公式取前 32 位
