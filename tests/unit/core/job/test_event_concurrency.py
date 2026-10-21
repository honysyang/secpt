"""PR-A 并发安全 + call_id 映射 单测。

覆盖：
- EventSink.write 多线程并发 append（行数完整 / 每行合法 JSON / seq 全局单调无重复）
- tool_call 事件带 call_id（与 tool_result 精确配对，不再依赖 FIFO）
- WarroomHooks call_id 映射：乱序 result 也能对映正确的 attempt
- Warroom 多线程并发写（busy 重试下无 "database is locked" 上抛、记录数完整）
- 事件行 agent_id/chain_id trace 字段（非空才写，缺省保持旧行格式）
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from secpt.core.job.event import EventSink
from secpt.core.memory.warroom import Warroom, WarroomHooks


def _read_lines(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


# ---------------- EventSink 并发 append ----------------


def test_concurrent_writes_are_atomic_and_seq_monotonic(tmp_path: Path) -> None:
    """8 线程 × 50 条并发写：行数完整、每行 JSON 合法、seq 恰为 1..400 无重复。"""
    sink = EventSink("jc", tmp_path, store=None)
    n_threads, per_thread = 8, 50
    errors: list[Exception] = []

    def worker(t: int) -> None:
        try:
            for i in range(per_thread):
                sink.write("step", {"n": t * per_thread + i})
        except Exception as e:  # pragma: no cover - 仅失败时触发
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    events = _read_lines(tmp_path / "jc" / "events.jsonl")
    assert len(events) == n_threads * per_thread  # 无丢行
    seqs = [e["seq"] for e in events]
    assert sorted(seqs) == list(range(1, n_threads * per_thread + 1))  # seq 全局唯一单调


def test_concurrent_writers_from_separate_sinks(tmp_path: Path) -> None:
    """模拟跨进程并发：两个独立 sink 实例（各带自己的 _seq 起点）写同一文件，
    flock + 尾读 seq 同步保证行完整且 seq 无重复。"""
    s1 = EventSink("jd", tmp_path, store=None)
    s1.write("step", {"n": 1})
    s2 = EventSink("jd", tmp_path, store=None)  # 初始化时读到 1 行，seq 从 1 起
    s2.write("step", {"n": 2})
    s1.write("step", {"n": 3})

    events = _read_lines(tmp_path / "jd" / "events.jsonl")
    assert len(events) == 3
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)  # 单调
    assert len(set(seqs)) == 3  # 无重复（s1 第三条经尾读同步跳过与 s2 的冲突）


# ---------------- call_id 映射 ----------------


def test_tool_call_event_carries_call_id(tmp_path: Path) -> None:
    """tool_call 事件带 call_id：与 tool_result 精确配对（事件流级 trace）。"""
    sink = EventSink("je", tmp_path, store=None)
    sink.on_tool("call-AAA", "bash", {"command": "ls"})
    sink.on_result("call-AAA", "done")
    events = _read_lines(tmp_path / "je" / "events.jsonl")
    tc = next(e for e in events if e["type"] == "tool_call")
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tc["payload"]["call_id"] == "call-AAA"
    assert tr["payload"]["call_id"] == "call-AAA"


class _StubInner:
    """记录透传调用的最内层 hooks 桩。"""

    def __init__(self) -> None:
        self.tool_calls: list[tuple[str, str]] = []
        self.results: list[str] = []

    def on_tool(self, call_id: str, name: str, args: dict) -> None:
        self.tool_calls.append((call_id, name))

    def on_result(self, call_id: str, out: str) -> None:
        self.results.append(call_id)


def test_warroom_hooks_call_id_mapping_out_of_order(tmp_path: Path) -> None:
    """乱序场景：A、B 两次 on_tool 后 result(B) 先回、result(A) 后回，
    attempt 的 result 必须各归其位（FIFO 方案在此场景下会错配）。"""
    warroom = Warroom(tmp_path / "w.sqlite", job_id="jw")
    inner = _StubInner()
    hooks = WarroomHooks(inner, warroom)

    hooks.on_tool("call-A", "bash", {"command": "cmdA"})
    hooks.on_tool("call-B", "bash", {"command": "cmdB"})
    hooks.on_result("call-B", "RESULT-B")
    hooks.on_result("call-A", "RESULT-A")

    assert inner.results == ["call-B", "call-A"]  # 透传保持完成序
    attempts = {a["action"]: a for a in warroom.list_attempts()}
    a_row = next(v for k, v in attempts.items() if "cmdA" in k)
    b_row = next(v for k, v in attempts.items() if "cmdB" in k)
    assert a_row["result"] == "RESULT-A"
    assert b_row["result"] == "RESULT-B"


def test_warroom_attempt_result_actually_updated(tmp_path: Path) -> None:
    """修正回归：on_result 的补记必须真正写入既有 attempts 行
    （原 INSERT OR IGNORE 撞 UNIQUE 后 result 永不落盘）。"""
    warroom = Warroom(tmp_path / "w.sqlite", job_id="jw")
    hooks = WarroomHooks(_StubInner(), warroom)

    hooks.on_tool("call-X", "bash", {"command": "whoami"})
    hooks.on_result("call-X", "root")

    rows = warroom.list_attempts()
    assert len(rows) == 1
    assert rows[0]["result"] == "root"


# ---------------- Warroom 并发写（busy 重试） ----------------


def test_warroom_concurrent_add_attempts(tmp_path: Path) -> None:
    """4 线程 × 25 条并发 add_attempt（不同 key）：无异常、无丢记录。"""
    warroom = Warroom(tmp_path / "w.sqlite", job_id="jw")
    errors: list[Exception] = []
    n_threads, per_thread = 4, 25

    def worker(t: int) -> None:
        try:
            for i in range(per_thread):
                warroom.add_attempt(f"action-{t}-{i}", target=f"http://127.0.0.1:{t}{i}")
        except Exception as e:  # pragma: no cover - 仅失败时触发
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []  # 无 "database is locked" 上抛（重试吸收）
    assert warroom.count_attempts() == n_threads * per_thread


def test_warroom_busy_retry_exhausts_and_raises(tmp_path: Path) -> None:
    """非 busy 异常直接上抛不被重试吞掉（重试只针对 locked/busy）。"""
    import sqlite3

    warroom = Warroom(tmp_path / "w.sqlite", job_id="jw")
    try:
        warroom._write(lambda conn: conn.execute("INSERT INTO no_such_table VALUES (1)"))
        raise AssertionError("应当上抛")
    except sqlite3.OperationalError as e:
        assert "no such table" in str(e)


# ---------------- trace 字段 ----------------


def test_event_lines_carry_agent_and_chain_trace(tmp_path: Path) -> None:
    """agent_id/chain_id 非空才写入事件行；缺省 sink 保持旧行格式（无这两个键）。"""
    sink = EventSink("jf", tmp_path, store=None, agent_id="exploit", chain_id="parent-1")
    sink.write("step", {"n": 1})
    plain = EventSink("jf", tmp_path, store=None)
    plain.write("step", {"n": 2})

    events = _read_lines(tmp_path / "jf" / "events.jsonl")
    assert events[0]["agent_id"] == "exploit"
    assert events[0]["chain_id"] == "parent-1"
    assert "agent_id" not in events[1]
    assert "chain_id" not in events[1]


def test_sink_repairs_partial_tail(tmp_path):
    """硬杀留下的无换行半行：初始化补换行，新事件不粘行、后续可正常解析。"""
    job_dir = tmp_path / "j"
    job_dir.mkdir()
    p = job_dir / "events.jsonl"
    p.write_text('{"seq": 1, "partial"', encoding="utf-8")  # 模拟半行
    sink = EventSink("j", tmp_path, store=None)
    sink.write("step", {"n": 2})
    raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
    assert len(raw) == 2
    assert raw[0] == '{"seq": 1, "partial"'  # 半行保留但被换行隔离
    assert json.loads(raw[1])["type"] == "step"


# ---------------- R3：flock 限时取锁 ----------------


def test_flock_wait_acquires_and_times_out(tmp_path):
    """_flock_wait：空闲时拿到锁返回 True；被持有时超时返回 False（不冻结）。"""
    fcntl = pytest.importorskip("fcntl")
    from secpt.core.job.event import _flock_wait

    p = tmp_path / "ev.jsonl"
    f1 = open(p, "a", encoding="utf-8")  # noqa: SIM115
    f2 = open(p, "a", encoding="utf-8")  # noqa: SIM115
    try:
        assert _flock_wait(f1.fileno(), timeout_s=2.0) is True  # 空闲：拿到
        import time as _t

        _t0 = _t.monotonic()
        assert _flock_wait(f2.fileno(), timeout_s=0.3) is False  # 被持：超时降级
        assert _t.monotonic() - _t0 < 2.0  # 真的没阻塞
        fcntl.flock(f1.fileno(), fcntl.LOCK_UN)
        assert _flock_wait(f2.fileno(), timeout_s=2.0) is True  # 释放后：拿到
        fcntl.flock(f2.fileno(), fcntl.LOCK_UN)
    finally:
        f1.close()
        f2.close()


def test_sink_write_survives_stale_lock(tmp_path, monkeypatch):
    """持锁方僵死（锁不释放）：EventSink 降级无锁写入，事件不丢。"""
    fcntl = pytest.importorskip("fcntl")
    import subprocess
    import sys

    monkeypatch.setenv("SECPT_EVENT_FLOCK_TIMEOUT_S", "0.3")  # 降级等待缩短，测试提速
    p = tmp_path / "ev.jsonl"
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import fcntl, sys, time\n"
        "f = open(sys.argv[1], 'a')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, str(holder), str(p)])
    try:
        import time as _t

        holder_ready = False
        for _ in range(50):  # 等子进程把锁拿上
            probe = open(p, "a", encoding="utf-8")  # noqa: SIM115
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder_ready = True
                break
            finally:
                probe.close()
            _t.sleep(0.1)
        assert holder_ready, "子进程未持有锁，测试前提不成立（旧实现这里会因 EBADF 误判）"
        sink = EventSink("stale", tmp_path, store=None)
        seq = sink.write("step", {"n": 1})
        assert seq >= 1
        lines = _read_lines(sink.events_path)
        assert lines[-1]["type"] == "step"
    finally:
        proc.kill()
