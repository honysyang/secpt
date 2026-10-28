"""W1-7 contract test：secpt 五命令 + main.py 兜底异常出口（§9）。

全部走 FakeLLM 离线脚本（LLM_PROVIDER=mock），SECPT_NMAP_BIN 指向不存在的
路径让 nmap 必落 E_TOOL_FAIL 回执——绝不依赖真实工具或网络。
"""

from __future__ import annotations

import io
import json
import pathlib
import re

import pytest

from secpt import main as main_mod
from secpt.errors import LlmError
from secpt.llms.client import StepOutput
from secpt.llms.fake import FakeLLM
from secpt.store import ledger
from secpt.store.migrate import connect

_SHA256 = re.compile(r"hash=([0-9a-f]{64})")


@pytest.fixture
def cli_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """五命令共用的隔离环境：db/raw 产物全落 tmp，nmap 二进制必缺。"""
    db = tmp_path / "harness.db"
    monkeypatch.setenv("SECPT_DB", str(db))
    monkeypatch.setenv("SECPT_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("SECPT_NMAP_BIN", str(tmp_path / "no-such-nmap"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    return db


def _run(cmd: list[str], env: pathlib.Path):
    """跑一轮 main 并抓 db 里唯一 task 行（run 模式只建一个 task）。"""
    rc = main_mod.main(cmd)
    conn = connect(env)
    try:
        task = conn.execute(
            "SELECT id, exit_kind FROM tasks ORDER BY created_at, rowid DESC"
        ).fetchone()
    finally:
        conn.close()
    return rc, (dict(task) if task is not None else None)


# ---- secpt run：FakeLLM 离线全流程 =============================================


def test_run_mock_flow_bubbles_and_normal_exit(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, task = _run(["run", "--goal", "对内网 10.0.0.1 做端口摸底"], cli_env)
    assert rc == 0
    assert task is not None and task["exit_kind"] == "normal"
    out = capsys.readouterr().out
    assert "【Orchestrator】step 1 开包" in out
    assert "【小票】nmap" in out  # 工具失败也落回执 → 小票摘要行
    assert "【出口】exit_kind=normal" in out
    assert "task_id=" in out and "run_id=" in out


def test_run_empty_goal_falls_to_chat_mode(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """WF1 失败路径：无 goal → 对话模式兜底，不开循环不建 task。"""
    rc, task = _run(["run", "--goal", "  "], cli_env)
    assert rc == 0 and task is None
    assert "对话模式" in capsys.readouterr().out


def test_run_abnormal_error_lands_ledger_then_nonzero_exit(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9 兜底链：库层上抛 LlmError → loop 先落账（abnormal/ended）→ main 非零退出。

    脚本首项必须是合法 goal JSON（enter 阶段消费、task 行落账），第二项在第 1 步
    structured 调用时抛 LlmError——异常必须发生在 step 阶段才能验证"先落账再出口"。
    """

    def boom(goal_text: str) -> FakeLLM:
        goal = json.dumps(
            {"target": goal_text[:60] or "(demo)", "objective": "o", "success_criteria": "s"},
            ensure_ascii=False,
        )
        return FakeLLM(
            [goal, LlmError("LLM 输出反复不合 schema", code="E_SCHEMA", context={"attempts": 3})]
        )

    monkeypatch.setattr(main_mod, "_demo_provider", boom)
    rc, task = _run(["run", "--goal", "x"], cli_env)
    assert rc == 1
    assert task is not None and task["exit_kind"] == "abnormal"
    captured = capsys.readouterr()
    assert "E_SCHEMA" in captured.err
    assert "abnormal_exit" in captured.err
    assert "Traceback" not in captured.err  # 兜底不带 traceback，绝不悬死


def test_unknown_provider_rejected_assembly_time(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    rc = main_mod.main(["run", "--goal", "x"])
    assert rc == 1
    assert "M1 仅支持 LLM_PROVIDER=mock" in capsys.readouterr().err


# ---- secpt resume：水位线续跑 =================================================


def _interrupted_task(env: pathlib.Path) -> str:
    """离场布景：落账一步水位线后"断电"——run 保持未 ended。"""
    conn = connect(env)
    try:
        task = ledger.start_task(conn, input_text="内网摸底", goal="10.0.0.1")
        ledger.start_run(conn, task.id, config={"objective": "o", "success_criteria": "s"})
        ledger.record_step(conn, task.id, ledger.latest_run(conn, task.id).id, 1, "{}")
        ledger.mark_step(conn, ledger.latest_run(conn, task.id).id, 1)
        return task.id
    finally:
        conn.close()


def test_resume_reuses_run_and_walks_on(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _interrupted_task(cli_env)
    monkeypatch.setattr(
        main_mod, "_demo_provider", lambda _g: FakeLLM([StepOutput(thought="续跑即收", done=True)])
    )
    rc = main_mod.main(["resume", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "step 2" in out
    conn = connect(cli_env)
    try:
        runs = conn.execute("SELECT id, status, step_count FROM runs").fetchall()
        task = conn.execute("SELECT exit_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert len(runs) == 1 and runs[0]["step_count"] == 2  # 复用原 run，不回放不重置
    assert runs[0]["status"] == "ended" and task["exit_kind"] == "normal"


def test_resume_ended_run_exits_gracefully(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = connect(cli_env)
    try:
        task = ledger.start_task(conn, input_text="完毕任务", goal="10.0.0.2")
        run = ledger.start_run(conn, task.id, config={"objective": "o", "success_criteria": "s"})
        ledger.end_run(conn, run.id)
    finally:
        conn.close()
    rc = main_mod.main(["resume", task.id])
    assert rc == 1
    assert "已正常收尾" in capsys.readouterr().err


def test_resume_unknown_task_exits_gracefully(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main_mod.main(["resume", "no-such-task"]) == 1
    assert "E_LEDGER" in capsys.readouterr().err


# ---- secpt tools / evidence / chat ============================================


def test_tools_lists_hashed_manifests(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main_mod.main(["tools"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "nmap" in out and "kind=mcp" in out
    assert "agent_run" in out and "kind=in-process" in out
    assert _SHA256.search(out) is not None  # manifest+hash 必须露出


def test_evidence_prints_landing_window(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(["run", "--goal", "对内网 10.0.0.3 做端口摸底"], cli_env)
    conn = connect(cli_env)
    try:
        task_id = str(conn.execute("SELECT id FROM tasks").fetchone()["id"])
    finally:
        conn.close()
    rc = main_mod.main(["evidence", task_id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "== evidence · task" in out
    assert "nmap | exit=" in out  # 小票行（工具失败回执也算小票）
    assert "findings" in out


def test_evidence_unknown_task_exits_nonzero(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main_mod.main(["evidence", "no-such-task"])
    assert rc == 1
    assert "E_LEDGER" in capsys.readouterr().err


def test_chat_single_turn_mock_answer(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main_mod.main(["chat", "你好"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "【AI】" in out  # 单轮直答，不进闭环


def test_chat_empty_input_no_op(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # 模拟空输入（pytest 默认封锁 stdin）
    assert main_mod.main(["chat"]) == 0
    assert capsys.readouterr().out != ""
