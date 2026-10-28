"""W1-5 水位线与断电续跑 contract test。

覆盖：mark_step 单调/幂等、resume_load 的 pick/拒绝/复建/残留忽略、
write_batch 多笔一事务的原子性、以及 kill -9 注入的端到端复原
（fork 子进程跑到第 4 步的 LLM 调用时 os._exit(9)，父进程按水位线续跑）。
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import NoReturn

import pytest

from secpt.core import checkpointer
from secpt.core.loop import PentestLoop, ToolOutput
from secpt.errors import LedgerError
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import ledger, migrate
from secpt.store.ledger import RunRow, TaskRow

GOAL_JSON = json.dumps(
    {
        "target": "10.0.0.7",
        "objective": "踩点确认暴露面",
        "success_criteria": "小票记录端口 banner",
    },
    ensure_ascii=False,
)


def _step_json(thought: str, *, calls: list[dict[str, object]], done: bool) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": calls, "done": done},
        ensure_ascii=False,
    )


class FakeDispatcher:
    """不执行真实工具：只署名一份固定回执（与 test_loop 同形，测试间不复用避免耦合）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        self.calls.append((name, dict(args)))
        return ToolOutput(
            tool_name=name,
            args=dict(args),
            exit_code=0,
            summary=f"{name} 完成",
            raw_ref=f"runs/fake/{name}.txt",
        )


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    c = migrate.connect(":memory:")
    yield c
    c.close()


def _seed(conn: sqlite3.Connection, *, steps: int = 0) -> tuple[TaskRow, RunRow]:
    """预置一段"曾活过"的账本：task + run（可选手推 steps 步并同步水位线）。"""
    task = ledger.start_task(conn, "断电续跑演练主机", goal="10.0.0.7")
    run = ledger.start_run(
        conn,
        task.id,
        config={"objective": "踩点确认暴露面", "success_criteria": "小票记录端口 banner"},
    )
    for n in range(1, steps + 1):
        ledger.record_step(
            conn, task.id, run.id, n, _step_json(f"断电前步{n}", calls=[], done=False)
        )
        checkpointer.mark_step(conn, run.id, n)
    return task, run


# ---- mark_step：水位线只进不退、幂等 ------------------------------------------


def test_mark_step_advances_waterline(conn: sqlite3.Connection) -> None:
    task, run = _seed(conn)
    checkpointer.mark_step(conn, run.id, 3)
    assert ledger.get_run(conn, run.id).step_count == 3
    assert task.id == ledger.get_task(conn, task.id).id  # get_task 顺带覆盖


def test_mark_step_rejects_retreat(conn: sqlite3.Connection) -> None:
    """水位线单调性：倒退被拒且账面不动（E_LEDGER 落错，绝不静默改账）。"""
    _, run = _seed(conn, steps=3)
    with pytest.raises(LedgerError, match="倒退"):
        checkpointer.mark_step(conn, run.id, 2)
    assert ledger.get_run(conn, run.id).step_count == 3


def test_mark_step_idempotent_same_n(conn: sqlite3.Connection) -> None:
    """同号重放（断电后重算同一步）是 no-op，不报错也不脏库。"""
    _, run = _seed(conn, steps=2)
    checkpointer.mark_step(conn, run.id, 2)
    assert ledger.get_run(conn, run.id).step_count == 2


# ---- resume_load：WF8 复建 ----------------------------------------------------


def test_resume_load_picks_latest_run_of_task(conn: sqlite3.Connection) -> None:
    task, run = _seed(conn, steps=2)
    state = checkpointer.resume_load(conn, task.id)
    assert state.waterline == 2
    assert state.run.id == run.id
    assert state.run.status == "running"
    assert state.task.goal == "10.0.0.7"  # 三问投影的账本锚


def test_resume_load_ignores_residue_beyond_waterline(conn: sqlite3.Connection) -> None:
    """其后残留忽略：水位线之后的 orphan step 只被无视，绝不回放或删除。"""
    task, run = _seed(conn, steps=1)
    ledger.record_step(
        conn, task.id, run.id, 2, _step_json("撞黑后的孤儿步残渣", calls=[], done=True)
    )
    state = checkpointer.resume_load(conn, task.id)
    assert state.waterline == 1
    # 残渣仍在账上（append-only 红线），只是续跑无视它、从水位线+1 重来
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 2


def test_resume_load_rejects_ended_run(conn: sqlite3.Connection) -> None:
    task, run = _seed(conn, steps=1)
    ledger.end_run(conn, run.id)
    with pytest.raises(LedgerError, match="正常收尾"):
        checkpointer.resume_load(conn, task.id)


def test_resume_load_without_run_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(LedgerError, match="查无 run"):
        checkpointer.resume_load(conn, "never-seen-task")


# ---- write_batch / step_transaction：收尾同一事务 ------------------------------


def test_write_batch_rolls_back_all_or_nothing(conn: sqlite3.Connection) -> None:
    """失败注入：批内小票+水位线写了一半被炸，整批回滚、一行不留。"""
    _, run = _seed(conn)
    with pytest.raises(RuntimeError, match="半路炸了"):
        with ledger.write_batch(conn):
            ledger.append_ticket(
                conn,
                run.task_id,
                run.id,
                "step_sim",
                "probe",
                {},
                0,
                "回执",
                "raw/x.txt",
                bus=None,
            )
            ledger.mark_step(conn, run.id, 1)
            raise RuntimeError("半路炸了")
    assert ledger.get_run(conn, run.id).step_count == 0
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_write_batch_rejects_nested_and_dirty(conn: sqlite3.Connection) -> None:
    """越权防线：批嵌套、带着未提交脏状态进场都被拒绝（宁可报错不吞账）。"""
    with pytest.raises(LedgerError, match="嵌套"):
        with ledger.write_batch(conn):
            with ledger.write_batch(conn):
                pass
    conn.execute(
        "INSERT INTO audit_log (task_id, actor, what, created_at) VALUES ('t', 'main', '脏探针', 0)"
    )
    with pytest.raises(LedgerError, match="脏"):
        with ledger.write_batch(conn):
            pass
    conn.rollback()  # 清掉脏探针，不留悬 transactions


def test_step_transaction_commits_waterline_with_tickets(conn: sqlite3.Connection) -> None:
    """WF8 收尾契约：steps + 小票 + step_count 同一笔事务，提交后整体可见。"""
    task, run = _seed(conn)
    with checkpointer.step_transaction(conn):
        step = ledger.record_step(
            conn, task.id, run.id, 1, _step_json("踩点", calls=[], done=False)
        )
        ledger.append_ticket(conn, task.id, run.id, step.id, "probe", {}, 0, "回执", "raw/p.txt")
        ledger.mark_step(conn, run.id, 1)
    assert ledger.get_run(conn, run.id).step_count == 1
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1


# ---- kill -9 注入：端到端复原（复用原 run、只复建不回放）------------------------


def _kill_on_step4() -> NoReturn:
    """FakeLLM 脚本第 5 项：子进程跑到第 4 步的 LLM 调用时被 kill -9。"""
    os._exit(9)


def _child_run_and_die(db_path: str) -> None:
    """子进程：完整跑 3 步（整批落账、水位线=3）后在第 4 步的 complete() 里被 kill。"""
    conn = migrate.connect(db_path)
    fake = FakeLLM(
        [
            GOAL_JSON,
            _step_json(
                "踩点 22 端口", calls=[{"name": "probe1", "args": {"port": "22"}}], done=False
            ),
            _step_json("再踩 80", calls=[], done=False),
            _step_json("还差一步", calls=[], done=False),
            _kill_on_step4,
        ]
    )
    loop = PentestLoop(conn, MainLlm(LlmClient(fake)), FakeDispatcher())
    loop.run("kill 注入主机")  # 永不返回：complete() 内 os._exit(9)


def test_kill9_then_resume_reuses_run(tmp_path: Path) -> None:
    """§10 断电续跑演练：3 步落账 → kill → 续跑复用原 run，step_no 从水位线+1。"""
    db = tmp_path / "ledger.db"
    child = multiprocessing.get_context("fork").Process(target=_child_run_and_die, args=(str(db),))
    child.start()
    child.join(timeout=15)
    assert child.exitcode == 9, f"子进程应死于 kill 注入，实际 exitcode={child.exitcode}"

    # kill 现场：水位线=3、step1~3 已整批提交、run 未收尾
    conn = migrate.connect(db)
    task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
    killed = conn.execute("SELECT * FROM runs").fetchone()
    assert killed["status"] == "running"
    assert killed["step_count"] == 3
    assert [r["step_no"] for r in conn.execute("SELECT step_no FROM steps ORDER BY step_no")] == [
        1,
        2,
        3,
    ]
    assert [t["tool_name"] for t in conn.execute("SELECT * FROM tickets")] == ["probe1"]

    # 续跑：脚本从 step4 开始（resume 不再走进场三问，三问从账本还原）
    fake = FakeLLM([_step_json("收工", calls=[], done=True)])
    loop = PentestLoop(conn, MainLlm(LlmClient(fake)), FakeDispatcher())
    result = loop.resume(task_id)
    assert result.finished_naturally is True
    assert result.run_id == killed["id"]  # 复用原 run：run id 跨 kill 连续
    run_after = ledger.get_run(conn, result.run_id)
    assert run_after.step_count == 4
    assert run_after.status == "ended"
    assert [r["step_no"] for r in conn.execute("SELECT step_no FROM steps ORDER BY step_no")] == [
        1,
        2,
        3,
        4,
    ]
    assert fake.call_count == 1  # 不回放：只对续跑的第一步调一次 LLM
    conn.close()
