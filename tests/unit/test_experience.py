"""W3-1 经验库测试：migration v3 / 唯一写口 / 锚点落闸 / 裁判三态回放 / Distiller。

分组：
- 迁移：0003 建表 + schema_version=3 + DELETE 触发器失败注入（经验永不满血搬家）；
- record_experience：store 唯一写口，audit actor=llmb，空锚点条目拒落 + audit 丢弃行；
- fetch_replay：环境过滤 + top_k + 只收 verified（M3-4 拍板④收紧，claimed/rejected 均不进回放）；
- Distiller：verify_state 由代码 derive（LLM 无权改判）、无候选不烧 LLM、
  idx 越界跳过、全部经 store 收口落账。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

import pytest

from secpt.errors import LedgerError, LlmError
from secpt.finalize.experience import Distiller
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.store import experience, ledger, migrate


@pytest.fixture()
def conn() -> Callable[[], sqlite3.Connection]:
    """每次测试都是一座新库（connect 内部完成 v1→v3 迁移）。"""
    return lambda: migrate.connect(":memory:")


def _seed_task(c: sqlite3.Connection) -> str:
    return ledger.start_task(c, "in", "goal").id


# ---- 迁移面 --------------------------------------------------------------------


def test_experience_schema_v3(conn: Callable[[], sqlite3.Connection]) -> None:
    c = conn()
    # 0003 建经验表在库版本 3 就位；库随 M3-3 推进到 4（0004 观测流）不回退。
    assert migrate.schema_version(c) >= 3
    cols = {r["name"] for r in c.execute("PRAGMA table_info(experience)").fetchall()}
    assert cols == {
        "id",
        "task_id",
        "environment",
        "verify_state",
        "useful_count",
        "summary",
        "command",
        "raw_ref",
        "created_at",
    }


def test_experience_delete_blocked_by_trigger(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """失败注入：绕过官门直写 DELETE 也被触发器当场拒掉（经验永不满血搬家）。"""
    c = conn()
    task_id = _seed_task(c)
    row = experience.record_experience(
        c, task_id, "lab", "claimed", "教训", "nmap -sV", "runs/a.txt"
    )
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError, match="E_APPEND_ONLY"):
        c.execute("DELETE FROM experience")
        c.commit()


def test_experience_update_allowed_for_m3_voting(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """UPDATE 放行：M3 判票要推进 useful_count（DELETE 才是红线）。"""
    c = conn()
    task_id = _seed_task(c)
    row = experience.record_experience(c, task_id, "lab", "claimed", "教训", None, "runs/a.txt")
    assert row is not None
    c.execute("UPDATE experience SET useful_count = 1")
    c.commit()
    fetched = c.execute("SELECT useful_count FROM experience").fetchone()
    assert fetched["useful_count"] == 1


# ---- 唯一写口面 ----------------------------------------------------------------


def test_record_experience_audits_llmb(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed_task(c)
    row = experience.record_experience(
        c, task_id, "lab", "verified", "22 端口对本环境常开", "nmap -p 22", "runs/b.txt"
    )
    assert row is not None
    assert row.task_id == task_id and row.environment == "lab"
    assert row.verify_state == "verified"
    assert row.useful_count == 0  # 初始 0 票：票数由 M3-4 判票写口（promote/decay）推进
    audits = c.execute("SELECT actor, what FROM audit_log WHERE task_id = ?", (task_id,)).fetchall()
    assert any(r["actor"] == "llmb" for r in audits)


def test_record_experience_empty_anchor_dropped_with_audit(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """锚点不可证条目拒落（不抛错）：落一行丢弃 audit，返回 None。"""
    c = conn()
    task_id = _seed_task(c)
    row = experience.record_experience(c, task_id, "lab", "claimed", "无锚经验", "cmd", "   ")
    assert row is None
    rows = c.execute("SELECT COUNT(*) FROM experience").fetchone()
    assert rows[0] == 0
    what = c.execute("SELECT what FROM audit_log WHERE actor = 'llmb'").fetchone()["what"]
    assert "锚" in what or "丢弃" in what or "拒" in what


def test_record_experience_rejects_illegal_state(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed_task(c)
    with pytest.raises(LedgerError, match="verify_state"):
        experience.record_experience(c, task_id, "lab", "满血", "s", None, "r")


# ---- 回放面 --------------------------------------------------------------------


def _fill(c: sqlite3.Connection, env: str, seq: int, state: str, summary: str) -> None:
    task_id = _seed_task(c)
    row = experience.record_experience(
        c, task_id, env, state, summary, f"cmd{seq}", f"runs/{seq}.txt"
    )
    assert row is not None


def test_fetch_replay_filters_environment_and_rejected(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    _fill(c, "lab", 1, "verified", "甲")
    _fill(c, "lab", 2, "claimed", "乙")
    _fill(c, "prod", 3, "verified", "丙")
    _fill(c, "lab", 4, "rejected", "丁")
    replay = experience.fetch_replay(c, "lab")
    # M3-4 收紧（拍板④）：只收 verified——乙（claimed 线索位取消）/丁（rejected）
    # 都不进包，Barrett 式收紧在 SQL 层收死；异环境不混、最新优先。
    assert [e.summary for e in replay] == ["甲"]
    assert [e.summary for e in experience.fetch_replay(c, "prod")] == ["丙"]
    assert experience.fetch_replay(c, "no-such-env") == []


def test_fetch_replay_top_k_truncates(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    for seq in range(7):
        _fill(c, "lab", seq, "verified", f"条{seq}")
    replay = experience.fetch_replay(c, "lab", top_k=3)
    assert [e.summary for e in replay] == ["条6", "条5", "条4"]  # 最新优先


# ---- Distiller 面（verify_state 由代码 derive）-----------------------------------


def _step_json(thought: str, done: bool = True) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": [], "done": done},
        ensure_ascii=False,
    )


def test_distiller_zero_candidates_skips_llm(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed_task(c)
    fake = FakeLLM([])
    entries = Distiller(LlmClient(fake)).distill(c, task_id, "lab")
    assert entries == []
    assert fake.call_count == 0  # 无可蒸馏物绝不烧 LLM
    what = c.execute("SELECT what FROM audit_log WHERE actor = 'llmb'").fetchall()
    assert any("0" in r["what"] for r in what)


def test_distiller_derives_verify_state_from_findings(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """claimed 与 verified 的 finding 各进一条：LLM 只写摘要，判态在代码。"""
    c = conn()
    task_id = _seed_task(c)
    ledger.append_finding(c, task_id, "悬置发现", "待裁决", "runs/a.txt")
    verified = ledger.append_finding(c, task_id, "实锤发现", "已确认", "runs/b.txt")
    ledger.set_finding_state(c, verified.id, "verified", method="manual")
    draft = json.dumps(
        {
            "entries": [
                {"idx": 0, "summary": "悬置经验（仅当线索）", "command": "nmap -A"},
                {"idx": 1, "summary": "实锤经验", "command": None},
            ]
        },
        ensure_ascii=False,
    )
    fake = FakeLLM([draft])
    entries = Distiller(LlmClient(fake)).distill(c, task_id, "lab")
    assert fake.call_count == 1
    assert [e.verify_state for e in entries] == ["claimed", "verified"]
    assert [e.summary for e in entries] == ["悬置经验（仅当线索）", "实锤经验"]
    assert entries[0].command == "nmap -A"
    assert [e.raw_ref for e in entries] == ["runs/a.txt", "runs/b.txt"]  # 锚点是事实非 LLM 产物


def test_distiller_hydrates_successful_tickets_as_claimed(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """成功小票 = claimed 候选（verify_state 如实：判票在 M3）；
    失败小票根本不进候选表——LLM 若试图给它立经验，idx 必然越界被跳过。"""
    c = conn()
    task_id = _seed_task(c)
    run = ledger.start_run(c, task_id)
    step = ledger.record_step(c, task_id, run.id, 1, _step_json("一步"))
    ledger.append_ticket(c, task_id, run.id, step.id, "nmap", {}, 0, "扫到 22", "runs/r1.txt")
    ledger.append_ticket(c, task_id, run.id, step.id, "nmap", {}, None, "失败", "")
    draft = json.dumps(
        {
            "entries": [
                {"idx": 1, "summary": "想把失败也归档", "command": None},
                {"idx": 0, "summary": "22 端口可达", "command": "nmap -p 22"},
            ]
        },
        ensure_ascii=False,
    )
    fake = FakeLLM([draft])
    entries = Distiller(LlmClient(fake)).distill(c, task_id, "lab")
    # idx=0 成功小票落 claimed；idx=1 失败小票不在候选表 → 越界跳过，永不进经验
    assert [e.summary for e in entries] == ["22 端口可达"]
    assert [e.verify_state for e in entries] == ["claimed"]
    final = c.execute(
        "SELECT what FROM audit_log WHERE actor = 'llmb' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()["what"]
    assert "越界跳过 1 条" in final


def test_distiller_skips_out_of_range_idx(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    c = conn()
    task_id = _seed_task(c)
    ledger.append_finding(c, task_id, "唯一发现", "d", "runs/a.txt")
    draft = json.dumps(
        {
            "entries": [
                {"idx": 9, "summary": "越界编造", "command": None},
                {"idx": -1, "summary": "负索引", "command": None},
                {"idx": 0, "summary": "正主", "command": None},
            ]
        },
        ensure_ascii=False,
    )
    entries = Distiller(LlmClient(FakeLLM([draft]))).distill(c, task_id, "lab")
    assert [e.summary for e in entries] == ["正主"]
    final = c.execute(
        "SELECT what FROM audit_log WHERE actor = 'llmb' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()["what"]
    assert "1 条" in final and "跳过" in final


def test_distiller_propagates_llm_error(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """库层不吞错：LlmError 原样上抛（主循环 hook 负责降级，Distiller 不背）。"""
    c = conn()
    task_id = _seed_task(c)
    ledger.append_finding(c, task_id, "d", "d", "runs/a.txt")
    with pytest.raises(LlmError):
        Distiller(
            LlmClient(FakeLLM([LlmError("脚本耗尽", code="E_FAKE_SCRIPT_EXHAUSTED")]))
        ).distill(c, task_id, "lab")


def test_distilled_rows_claimed_until_voted(
    conn: Callable[[], sqlite3.Connection],
) -> None:
    """One peace：distill 落 experience 行（audit llmb）；M3-4 收紧（拍板④）下
    claimed 判票前不进回放位——promote_claimed 升 verified 后才可见。"""
    c = conn()
    task_id = _seed_task(c)
    ledger.append_finding(c, task_id, "可复用发现", "d", "runs/a.txt")
    draft = json.dumps({"entries": [{"idx": 0, "summary": "老经验一句话", "command": "nmap"}]})
    Distiller(LlmClient(FakeLLM([draft]))).distill(c, task_id, "lab")
    row = c.execute("SELECT verify_state, raw_ref FROM experience").fetchone()
    assert row["verify_state"] == "claimed"
    assert experience.fetch_replay(c, "lab") == []  # 未判票：claimed 不进回放位
    experience.promote_claimed(c, task_id, "lab", row["raw_ref"])
    assert [e.summary for e in experience.fetch_replay(c, "lab")] == ["老经验一句话"]
