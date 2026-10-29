"""W1-7 contract test：secpt 五命令 + main.py 兜底异常出口（§9）。

全部走 FakeLLM 离线脚本（LLM_PROVIDER=mock），SECPT_NMAP_BIN 指向不存在的
路径让 nmap 必落 E_TOOL_FAIL 回执——绝不依赖真实工具或网络。
"""

from __future__ import annotations

import io
import json
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from secpt import main as main_mod
from secpt.core.mission_board import Mission
from secpt.errors import LlmError
from secpt.llms.client import StepOutput
from secpt.llms.fake import FakeLLM
from secpt.llms.route import Route
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
    assert "【主LLM】第 1/" in out and "开始决策" in out
    assert "【主LLM → nmap】" in out  # 工具失败也落回执 → 小票摘要行
    assert "【出口】exit_kind=normal" in out
    assert "task_id=" in out and "run_id=" in out


def test_run_warns_unfinished_run(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M5-28：账上留着 running 的 run → 启动时提示可 resume（不自动接管）。"""
    conn = connect(cli_env)
    stale = ledger.start_task(conn, input_text="上次被打断", goal="10.0.0.9")
    ledger.start_run(conn, stale.id, config={"objective": "o", "success_criteria": "s"})
    conn.close()

    rc, _task = _run(["run", "--goal", "对内网 10.0.0.1 做端口摸底"], cli_env)
    out = capsys.readouterr().out
    assert rc == 0
    assert f"未收尾 run（task_id={stale.id}）" in out
    assert f"secpt resume {stale.id}" in out


def test_board_resolver_is_thread_safe(cli_env: pathlib.Path) -> None:
    """M5-30：并发派单时 submit 发生在别的线程，锚点解析不能借主线程连接。"""
    conn = connect(cli_env)
    task = ledger.start_task(conn, input_text="锚点线程安全", goal="10.0.0.1")
    conn.close()

    board = main_mod._board(connect(cli_env))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            outer = pool.submit(
                board.submit, Mission(persona="p", goal="线程里派单", step_budget=1)
            )
            result = outer.result(timeout=30).result(timeout=30)
    finally:
        board.shutdown()
    assert task.id  # 账本里确有可解析的 task
    assert result.ok is True


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

    monkeypatch.setattr(main_mod, "_mock_run_script", boom)
    rc, task = _run(["run", "--goal", "x"], cli_env)
    assert rc == 1
    assert task is not None and task["exit_kind"] == "abnormal"
    captured = capsys.readouterr()
    assert "E_SCHEMA" in captured.err
    assert "abnormal_exit" in captured.err
    assert "Traceback" not in captured.err  # 兜底不带 traceback，绝不悬死


def test_openai_provider_missing_env_rejected_assembly_time(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4-1 契约：openai 档缺任一 env → 装配期 E_CONFIG 拒跑，绝不静默降 mock。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    for name in ("SECPT_LLM_BASE_URL", "SECPT_LLM_API_KEY", "SECPT_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    rc = main_mod.main(["run", "--goal", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "E_CONFIG" in err
    assert "SECPT_LLM_BASE_URL" in err
    assert "SECPT_LLM_API_KEY" in err
    assert "SECPT_LLM_MODEL" in err


def test_openai_full_run_against_local_fake_server(
    cli_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4 验收 1：openai 兼容档可跑完整 run——本地假 server 演示，零联网。

    与 FakeLLM 演示同一条流水线（进场三问 → nmap 摸扫回执 → 收工 → 报告摘要），
    唯一差别是聪明来自 HTTP（openai 协议）而非出厂脚本——密钥只进请求头
    （Authorization: Bearer …），请求逐单留痕可核。
    """
    from tests.unit.test_openai_provider import _API_KEY, _MODEL, _FakeOpenAIServer

    server = _FakeOpenAIServer()
    server.start()
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SECPT_LLM_BASE_URL", server.base_url)
    monkeypatch.setenv("SECPT_LLM_API_KEY", _API_KEY)
    monkeypatch.setenv("SECPT_LLM_MODEL", _MODEL)
    try:
        # 与 _mock_run_script 同序：goal JSON → 扫描步 → 收工 → 报告摘要
        goal_json = json.dumps(
            {
                "target": "对内网 10.0.0.9 做端口摸底"[:60],
                "objective": "o",
                "success_criteria": "s",
            },
            ensure_ascii=False,
        )
        server.queue(goal_json)
        server.queue(
            json.dumps(
                {
                    "thought": "摸扫一圈端口（openai 档演示步）",
                    "tool_calls": [
                        {"name": "nmap", "args": {"target": "(demo)", "ports": "22,80"}}
                    ],
                },
                ensure_ascii=False,
            )
        )
        server.queue(json.dumps({"thought": "收工", "tool_calls": [], "done": True}))
        server.queue(json.dumps({"summary": "openai 假 server 演示收束。"}, ensure_ascii=False))
        rc = main_mod.main(["run", "--goal", "对内网 10.0.0.9 做端口摸底"])
    finally:
        server.stop()
    assert rc == 0
    out = capsys.readouterr().out
    assert "【出口】exit_kind=normal" in out
    assert server.requests, "openai 档竟零请求——聪明没走 HTTP"
    assert f"Bearer {_API_KEY}" in server.auth_headers[0]  # 密钥只进请求头


def test_unknown_provider_rejected_assembly_time(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    rc = main_mod.main(["run", "--goal", "x"])
    assert rc == 1
    assert "deepseek" in capsys.readouterr().err


def test_max_steps_from_profile_limits_the_run(
    cli_env: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """M5 关键参数：profile [budget].max_steps 真的掐主循环步数线（1 步即撞限）。"""
    profile = tmp_path / "profile.toml"
    profile.write_text("[budget]\nmax_steps = 1\n", encoding="utf-8")
    monkeypatch.setenv("SECPT_PROFILE", str(profile))
    rc = main_mod.main(["run", "--goal", "对内网 10.0.0.7 做端口摸底"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "steps=1 exit_kind=stalled" in out


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
        main_mod,
        "_mock_run_script",
        lambda _g: FakeLLM([StepOutput(thought="续跑即收", done=True)]),
    )
    rc = main_mod.main(["resume", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "第 2/200 步" in out
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


def test_assets_lists_six_libraries(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """六库可查（M5-14）：文件资产型逐条枚举；数据/进程型给指引不重复实现。"""
    rc = main_mod.main(["assets"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "prompts（" in out and "coach.review" in out
    assert "skills（" in out and "port-recon" in out
    assert "roles（" in out and "recon" in out
    assert "knowledge（" in out and "port-scan" in out
    assert "`secpt metrics`" in out and "`secpt tools`" in out


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
    assert "-- plans --" in out  # M5-17：规划段进证据检查窗
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
    assert "【secpt·对话】" in out and "【你】你好" in out  # 角色标签 + 用户回显


def test_chat_empty_input_no_op(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # 模拟空输入（pytest 默认封锁 stdin）
    assert main_mod.main(["chat"]) == 0
    assert capsys.readouterr().out != ""


# ---- M4-4 审批代理装配（prompt 档出代理，未装档 None） ===========================


def test_approval_broker_only_in_prompt_mode(
    cli_env: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prompt 档装配 ApprovalBroker（delegate=CliApprover）；off/缺段 = None 守恒。"""
    profile = tmp_path / "profile.toml"

    def set_profile(text: str) -> None:
        profile.write_text(text, encoding="utf-8")
        monkeypatch.setenv("SECPT_PROFILE", str(profile))

    set_profile("[guard]\nhitl_mode = 'prompt'\n")
    broker = main_mod._approval_broker()
    assert isinstance(broker, main_mod.ApprovalBroker)
    assert broker.kind == "human"  # 署名透传真审批人，代理不冒充

    set_profile("[guard]\nhitl_mode = 'off'\n")
    assert main_mod._approval_broker() is None

    set_profile("")  # 缺 [guard] 段 = 未装代理
    assert main_mod._approval_broker() is None


def test_broker_audit_sink_writes_main_actor_row(cli_env: pathlib.Path) -> None:
    """裁决侧落账：actor=main + 批准/拒绝文案（与 workerA 发起半边分层可辨）。"""
    conn = connect(str(cli_env))
    task = ledger.start_task(conn, input_text="m44", goal="摸扫")
    conn.close()

    main_mod._broker_audit_sink("nmap", True, "human")
    main_mod._broker_audit_sink("nmap", False, "human")

    conn = connect(str(cli_env))
    rows = conn.execute(
        "SELECT actor, what FROM audit_log WHERE task_id=? AND what LIKE '%审批代理裁决%'",
        (task.id,),
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["main", "main"]
    assert "批准" in rows[0][1] and "nmap" in rows[0][1]
    assert "拒绝" in rows[1][1]


# ---- run 统一入口（M5-22：主智能体自动判断任务/对话） --------------------------


def test_run_auto_routes_to_pentest(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        main_mod,
        "_route_provider",
        lambda text: FakeLLM([Route(mode="pentest", goal="对内网 10.0.0.1 做端口摸底")]),
    )
    rc = main_mod.main(["run", "帮我看看 10.0.0.1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "【模式】自动判断为渗透任务" in out
    assert "exit_kind=normal" in out


def test_run_auto_routes_to_chat_without_task(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "_route_provider", lambda text: FakeLLM([Route(mode="chat")]))
    monkeypatch.setattr(
        main_mod, "_chat_provider", lambda: FakeLLM(["我是 secpt，这套渗透测试系统的助手。"])
    )
    rc = main_mod.main(["run", "什么是 XSS"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "【模式】自动判断为对话（不落账）" in out
    assert "【secpt·对话】我是 secpt，这套渗透测试系统的助手。" in out
    conn = connect(cli_env)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_mock_run_with_successful_tool_prints_report(
    cli_env: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """工具真成功时蒸馏会多调一次 LLM：双用途尾项保证终场报告不降级。"""
    nmap = tmp_path / "fake_nmap.sh"
    nmap.write_text('#!/bin/sh\necho "22/tcp open ssh"\n', encoding="utf-8")
    nmap.chmod(0o755)
    monkeypatch.setenv("SECPT_NMAP_BIN", str(nmap))
    rc = main_mod.main(["run", "--goal", "对内网 10.0.0.1 做端口摸底"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "report_degraded" not in captured.err
    assert "# 终场报告" in captured.out


def test_chat_sends_pentest_identity_prompt(
    cli_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5-23：对话模式必须带身份系统提示，不能自我介绍成通用助手。"""
    fake = FakeLLM(["好的，我来帮你"])
    monkeypatch.setattr(main_mod, "_chat_provider", lambda: fake)
    assert main_mod.main(["chat", "你好"]) == 0
    messages = fake.calls[0]
    assert messages[0].role == "system"
    assert "secpt" in messages[0].content
    assert "授权" in messages[0].content  # 边界纪律也在系统提示里
    assert messages[1].role == "user" and messages[1].content == "你好"


def test_run_prints_security_posture(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M5-25：默认关的开关必须在第一屏可见。"""
    main_mod.main(["run", "--goal", "x"])
    out = capsys.readouterr().out
    assert "【安全态势】" in out
    assert "裁判=" in out and "探针=" in out and "隔离=" in out and "Guard=" in out


def test_posture_warns_when_judge_off(
    cli_env: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    monkeypatch.setenv("SECPT_PROFILE", str(tmp_path / "no-such-profile.toml"))
    main_mod.main(["run", "--goal", "x"])
    out = capsys.readouterr().out
    assert "裁判=关" in out
    assert "注意：裁判关闭" in out


def test_mock_run_prints_step_decision(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M5-26/29：CLI 展示每步思考与工具，并带该步耗时。"""
    main_mod.main(["run", "--goal", "对内网 10.0.0.1 做端口摸底"])
    out = capsys.readouterr().out
    assert "第 1 步决策" in out
    assert "摸扫一圈端口（演示步）（工具：nmap）" in out
    assert "【主LLM → nmap】" in out
    assert "【出口】exit_kind=normal" in out


def test_unhandled_exception_exits_structured_no_traceback(
    cli_env: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5-34：SecptError 家族之外的裸异常也走结构化出口——JSON 一行 + 非零退出，
    绝不裸 traceback（审计可解析，问题可定位）。"""
    monkeypatch.setattr(
        main_mod,
        "_cmd_assets",
        lambda _args: (_ for _ in ()).throw(RuntimeError("磁盘满")),
    )
    rc = main_mod.main(["assets"])
    err = capsys.readouterr().err
    assert rc == 1
    # stderr 整体是一行可解析的结构化 JSON（trace 进 context 供审计），
    # 而不是裸 traceback 甩给终端。
    payload = json.loads(err.strip())
    assert payload["code"] == "E_UNHANDLED"
    assert "RuntimeError" in payload["message"]
    assert "Traceback" in payload["context"]["trace"]


def test_connect_sets_busy_timeout(tmp_path: pathlib.Path) -> None:
    """M5-34：多线程写库前先给 busy_timeout（默认 0 = 锁冲突立即炸）。"""
    db = tmp_path / "busy.db"
    conn = connect(db)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 30000
    finally:
        conn.close()
