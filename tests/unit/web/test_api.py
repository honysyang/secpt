"""Web 控制台单测：payload 构造 / 事件游标 / 上传 / 配置 / 服务烟雾。"""

from __future__ import annotations

import base64
import json
import threading
import time

import httpx
import pytest

from secpt.web import api
from secpt.web.server import make_server


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    return tmp_path


def _write_events(env, job_id: str, events: list[dict]) -> None:
    from secpt.core.job.resume import jobs_dir

    jdir = jobs_dir() / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, ev in enumerate(events, 1):
        lines.append(json.dumps({"seq": i, "job_id": job_id, "ts": time.time(), "round": 0, **ev}))
    (jdir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# payload 构造
# ---------------------------------------------------------------------------


def test_overview_counts(env) -> None:
    payload = api.overview_payload()
    assert set(payload) >= {"assets", "findings", "experiences", "jobs"}
    assert payload["jobs"] >= 0


def test_tools_payload_has_builtin_and_skills(env) -> None:
    payload = api.tools_payload()
    names = {t["name"] for t in payload["builtin"]}
    assert {"bash", "read_file", "list_dir", "write_file"} <= names
    assert isinstance(payload["plugins"], list) and payload["plugins"]
    assert any("SSRF" in s["name"] for s in payload["skills"])


def test_experiences_payload_shape(env) -> None:
    payload = api.experiences_payload()
    assert "experiences" in payload and "total" in payload


def test_config_save_load_apply(env, monkeypatch) -> None:
    monkeypatch.delenv("SECPT_LOGISTICS_WORKERS", raising=False)
    result = api.save_config({"logistics_workers": "3", "bogus_key": "x"})
    assert result["saved"] == {"logistics_workers": "3"}  # 白名单外剔除
    import os

    assert os.environ.get("SECPT_LOGISTICS_WORKERS") == "3"
    assert api.config_payload()["saved"]["logistics_workers"] == "3"


def test_events_payload_cursor(env) -> None:
    _write_events(
        env,
        "j1",
        [
            {"type": "status_change", "payload": {"status": "running"}},
            {"type": "finding", "payload": {"id": "f1", "kind": "flag", "value": "FLAG{x}"}},
            {"type": "error", "payload": {"message": "boom"}},
        ],
    )
    full = api.events_payload("j1", after=0)
    assert full["cursor"] == 3 and len(full["events"]) == 3
    inc = api.events_payload("j1", after=2)
    assert [e["type"] for e in inc["events"]] == ["error"]
    assert inc["cursor"] == 3


def test_events_payload_missing_job(env) -> None:
    payload = api.events_payload("nope", after=0)
    assert payload == {"events": [], "cursor": 0, "has_more": False}


def test_workspace_payload_from_events(env) -> None:
    _write_events(
        env,
        "w1",
        [
            {"type": "status_change", "payload": {"status": "running"}},
            {
                "type": "assistant",
                "payload": {"content": "拿到 FLAG{ws_test} 收工", "tool_calls": []},
            },
            {
                "type": "finding",
                "payload": {"id": "f1", "kind": "credential", "value": "root:toor"},
            },
        ],
    )
    payload = api.workspace_payload("w1")
    assert "FLAG{ws_test}" in payload["flags"]
    assert any(f["value"] == "root:toor" for f in payload["findings"])
    assert payload["intents"] == []  # 无 frontier 库不报错


def test_save_uploads(env) -> None:
    files = [
        {"name": "notes.txt", "data_b64": base64.b64encode(b"hello").decode()},
        {"name": "../../evil.txt", "data_b64": base64.b64encode(b"x").decode()},
        {"name": "empty.bin", "data_b64": ""},
    ]
    paths = api.save_uploads("u1", files)
    assert len(paths) == 2  # 空/非法被剔除
    assert all(str(p).startswith(str(env)) for p in paths)  # 路径穿越被净化
    from pathlib import Path

    assert Path(paths[0]).read_text() == "hello"


# ---------------------------------------------------------------------------
# 任务提交（注入假 runner，不起真实引擎）
# ---------------------------------------------------------------------------


def test_create_task_rejects_empty(env) -> None:
    assert "error" in api.create_task("  ")


def test_create_task_spawns_runner(env, monkeypatch) -> None:
    from secpt.core.job.models import TaskSpec

    seen: dict = {}

    def fake_run(job_id, spec, llm, confirm_event=None):
        seen["job_id"] = job_id
        seen["spec"] = spec
        seen["llm"] = llm

    monkeypatch.setattr(api, "_run_job", fake_run)
    result = api.create_task(
        "测试目标",
        profile="ctf",
        files=[{"name": "a.txt", "data_b64": base64.b64encode(b"data").decode()}],
        fake=True,
        budget_steps=7,
    )
    assert result["job_id"] and result["mode"] == "fake"
    deadline = time.time() + 5
    while seen.get("job_id") != result["job_id"] and time.time() < deadline:
        time.sleep(0.05)
    spec: TaskSpec = seen["spec"]
    assert spec.objective == "测试目标"
    assert spec.budget.steps == 7
    assert spec.artifacts and "a.txt" in spec.artifacts[0]
    assert seen["llm"] is not None  # fake 模式传 FakeLLM


# ---------------------------------------------------------------------------
# 服务烟雾（stdlib HTTP 起在随机端口）
# ---------------------------------------------------------------------------


@pytest.fixture
def server(env):
    srv = make_server("127.0.0.1", 0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()


def test_http_overview_and_static(server) -> None:
    r = httpx.get(f"{server}/api/overview", timeout=5)
    assert r.status_code == 200 and "assets" in r.json()
    idx = httpx.get(f"{server}/", timeout=5)
    assert idx.status_code == 200 and "secpt" in idx.text
    js = httpx.get(f"{server}/static/app.js", timeout=5)
    assert js.status_code == 200 and "renderFeed" in js.text


def test_http_traversal_blocked(server) -> None:
    r = httpx.get(f"{server}/static/../../pyproject.toml", timeout=5)
    assert r.status_code in (403, 404)


def test_http_jobs_rejects_empty(server) -> None:
    r = httpx.post(
        f"{server}/api/jobs",
        json={"objective": "  "},
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    assert r.status_code == 400


def test_http_404(server) -> None:
    r = httpx.get(f"{server}/api/nope", timeout=5)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 对话中发布任务：direct 注入 + 收敛后自动续接（同一作战地图）
# ---------------------------------------------------------------------------


def _mk_record(job_id: str, objective: str = "拿下目标"):
    from secpt.core.job.models import JobRecord, TaskSpec
    from secpt.core.job.store import JobStore

    spec = TaskSpec(job_id=job_id, task_type="ctf", objective=objective)
    JobStore().upsert_job(JobRecord(job_id=job_id, spec=spec, status="succeeded"))


def _mk_pool(job_id: str) -> None:
    from secpt.core.orchestrator.frontier import Frontier, frontier_db_path

    fr = Frontier(frontier_db_path(job_id))
    fr.add("初始任务", goal="侦察", done_criteria="有结论", priority=5)


def test_inject_resumes_converged_task(env, monkeypatch) -> None:
    """任务收敛后对话追问：direct 意图入池 + 同 job_id 自动续接编排。"""
    seen: dict = {}

    def fake_run(job_id, spec, llm, confirm_event=None):
        seen["job_id"] = job_id

    monkeypatch.setattr(api, "_run_job", fake_run)
    _mk_record("conv-1")
    _mk_pool("conv-1")
    result = api.inject_message("conv-1", "再看看 8080 端口")
    assert result.get("ok") and result.get("resumed") is True
    assert seen.get("job_id") == "conv-1"  # 同一作战地图，不新建任务
    # direct 意图确实入池
    from secpt.core.orchestrator.frontier import Frontier, frontier_db_path

    fr = Frontier(frontier_db_path("conv-1"))
    directs = [r for r in fr.list_all(limit=50) if r.get("source") == "direct"]
    assert len(directs) == 1 and "8080" in directs[0]["goal"]


def test_inject_no_resume_while_active(env, monkeypatch) -> None:
    """任务运行中追问：只入池不重启（避免双编排撞同一张地图）。"""
    called: list[str] = []

    def fake_run(job_id, spec, llm, confirm_event=None):
        called.append(job_id)

    monkeypatch.setattr(api, "_run_job", fake_run)
    _mk_record("act-1")
    _mk_pool("act-1")
    api.JOBS["act-1"] = {"thread": threading.current_thread(), "started_at": time.time()}
    try:
        result = api.inject_message("act-1", "补充：注意边界")
        assert result.get("ok") and result.get("resumed") is False
        assert called == []
    finally:
        api.JOBS.pop("act-1", None)


def test_inject_no_pool_errors(env) -> None:
    assert "error" in api.inject_message("ghost-9", "你好")


# ---------------------------------------------------------------------------
# 接战确认闸（v11）：auto_dispatch 选项 / confirm_task / 待确认期 inject 拒收
# ---------------------------------------------------------------------------


def _mk_record_status(job_id: str, status: str, objective: str = "拿下目标"):
    from secpt.core.job.models import JobRecord, TaskSpec
    from secpt.core.job.store import JobStore

    spec = TaskSpec(job_id=job_id, task_type="ctf", objective=objective)
    JobStore().upsert_job(JobRecord(job_id=job_id, spec=spec, status=status))


def test_create_task_auto_dispatch_option(env, monkeypatch):
    """auto_dispatch=False → 接战确认闸：编排挂起等 /confirm；True/缺省 → 直接开打。"""

    def fake_run(job_id, spec, llm, confirm_event=None):
        pass

    monkeypatch.setattr(api, "_run_job", fake_run)
    r1 = api.create_task("拿下 test 站", fake=True, auto_dispatch=False)
    assert "job_id" in r1
    ev = api.JOBS[r1["job_id"]].get("confirm_event")
    assert isinstance(ev, threading.Event) and not ev.is_set()
    api.JOBS.pop(r1["job_id"], None)

    r2 = api.create_task("拿下 test 站", fake=True, auto_dispatch=True)
    assert api.JOBS[r2["job_id"]].get("confirm_event") is None
    api.JOBS.pop(r2["job_id"], None)


def test_confirm_task_releases_gate(env) -> None:
    ev = threading.Event()
    api.JOBS["gate-9"] = {"thread": threading.current_thread(), "confirm_event": ev}
    try:
        result = api.confirm_task("gate-9")
        assert result.get("ok") is True and ev.is_set()
        # 重复确认：事件已被消费，第二次报错
        assert "error" in api.confirm_task("gate-9")
    finally:
        api.JOBS.pop("gate-9", None)


def test_confirm_task_unknown_job_errors(env) -> None:
    assert "error" in api.confirm_task("ghost-1")


def test_inject_blocked_while_awaiting_confirm(env, monkeypatch) -> None:
    """接战单待确认期：对话消息拒收，引导先确认（避免意图绕过确认直接入池）。"""
    _mk_record_status("wait-1", "awaiting_confirm")
    result = api.inject_message("wait-1", "先帮我看看 8080")
    assert "error" in result and "接战单" in result["error"]


# ---------------------------------------------------------------------------
# _run_job 收尾（真实运行暴露：非法终态 "completed" 被吞 → 任务卡 running；
# save_config 白名单丢弃 delivery:* → 交付判定永不落盘）
# ---------------------------------------------------------------------------


def test_run_job_sets_valid_terminal_status_and_delivery(env, monkeypatch):
    """正常收口：status→succeeded（JOB_STATUSES 合法值），delivery 写 job 目录。"""

    async def fake_run_task(spec, **kw):
        return {"job_id": spec.job_id, "mode": "frontier", "delivery": {"accepted": True}}

    monkeypatch.setattr("secpt.core.orchestrator.dag.run_task", fake_run_task)
    _mk_record_status("fin-1", "running")
    from secpt.core.job.store import JobStore

    api._run_job("fin-1", JobStore().get_job("fin-1").spec, llm=None)
    rec = JobStore().get_job("fin-1")
    assert rec.status == "succeeded"  # 不是 "completed"（upsert 会 ValueError）
    payload = api.workspace_payload("fin-1")
    assert (payload.get("delivery") or {}).get("accepted") is True
    from secpt.core.job.resume import jobs_dir

    assert (jobs_dir() / "fin-1" / "delivery.json").is_file()


def test_run_job_marks_failed_on_exception(env, monkeypatch):
    """编排抛异常：status→failed，事件流留 error，不再卡 running。"""

    async def boom(spec, **kw):
        raise RuntimeError("orchestrator dead")

    monkeypatch.setattr("secpt.core.orchestrator.dag.run_task", boom)
    _mk_record_status("fin-2", "running")
    from secpt.core.job.store import JobStore

    api._run_job("fin-2", JobStore().get_job("fin-2").spec, llm=None)
    assert JobStore().get_job("fin-2").status == "failed"


def test_web_host_env_override(monkeypatch) -> None:
    """SECPT_WEB_HOST 作为默认监听地址（远程部署不必每次带 --host）；显式参数仍优先。"""
    from typer.testing import CliRunner

    from secpt.cli.app import app

    seen: dict = {}
    import secpt.web.server as srv

    monkeypatch.setattr(srv, "serve", lambda *, host, port: seen.update(host=host, port=port))
    monkeypatch.setenv("SECPT_WEB_HOST", "0.0.0.0")
    r = CliRunner().invoke(app, ["web", "--port", "8081"])
    assert r.exit_code == 0, r.output
    assert seen == {"host": "0.0.0.0", "port": 8081}

    seen.clear()
    r = CliRunner().invoke(app, ["web", "--host", "127.0.0.1"])
    assert r.exit_code == 0, r.output
    assert seen["host"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# 文件卡回源：GET /api/jobs/{id}/file（事件流 args 截断，正文只能回磁盘）
# ---------------------------------------------------------------------------


def test_job_file_payload_reads_workspace(env) -> None:
    ws = env / "jobs" / "job-1-iabcd" / "ws"
    (ws / "recon").mkdir(parents=True)
    (ws / "recon" / "probe.py").write_text("print('ok')\n", encoding="utf-8")

    d = api.job_file_payload("job-1", sub="job-1-iabcd", path="recon/probe.py")
    assert d["ok"] is True
    assert d["content"] == "print('ok')\n"
    assert d["bytes"] == 12

    # 父 job 自己的 ws 也允许（sub 缺省）
    (ws.parent / "ws" / "note.md").write_text("hi", encoding="utf-8")
    assert api.job_file_payload("job-1-iabcd", path="note.md")["ok"] is True


def test_job_file_payload_rejects_escape_and_foreign_subjob(env) -> None:
    ws = env / "jobs" / "job-1-iabcd" / "ws"
    ws.mkdir(parents=True)
    (ws / "a.txt").write_text("x", encoding="utf-8")

    assert "error" in api.job_file_payload("job-1", sub="job-1-iabcd", path="../../etc/passwd")
    assert "error" in api.job_file_payload("job-1", sub="other-job", path="a.txt")
    assert "error" in api.job_file_payload("job-1", sub="job-1-iabcd", path="missing.txt")
    assert "error" in api.job_file_payload("job-1", sub="job-1-iabcd", path="")


def test_delete_job_cancels_nonterminal(env, monkeypatch) -> None:
    """删除 queued/pending 任务也要先取消，否则线程照常启动、派生孤儿子任务。"""
    from secpt.core.job.resume import jobs_dir
    from secpt.core.job.store import JobStore

    _mk_record_status("del-1", "queued")
    (jobs_dir() / "del-1").mkdir(parents=True, exist_ok=True)
    called: list[str] = []
    monkeypatch.setattr(api, "cancel_task", lambda jid: called.append(jid) or {"ok": True})

    assert api.delete_job("del-1").get("ok") is True
    assert called == ["del-1"]
    assert JobStore().get_job("del-1") is None


def test_delete_job_terminal_skips_cancel(env, monkeypatch) -> None:
    from secpt.core.job.resume import jobs_dir

    _mk_record_status("del-2", "succeeded")
    (jobs_dir() / "del-2").mkdir(parents=True, exist_ok=True)
    called: list[str] = []
    monkeypatch.setattr(api, "cancel_task", lambda jid: called.append(jid) or {"ok": True})

    assert api.delete_job("del-2").get("ok") is True
    assert called == []


def test_workspace_payload_includes_result_md(env) -> None:
    """任务收口落 result.md → workspace 载荷直接可读（前端结果卡展示）。"""
    from secpt.core.job.resume import jobs_dir

    jdir = jobs_dir() / "res-1"
    jdir.mkdir(parents=True, exist_ok=True)
    (jdir / "result.md").write_text("# 任务结果\n- 战果：FLAG{x}", encoding="utf-8")
    out = api.workspace_payload("res-1")
    assert "FLAG{x}" in (out.get("result") or "")


def test_workspace_payload_result_empty_when_missing(env) -> None:
    out = api.workspace_payload("res-missing")
    assert out.get("result") == ""


def test_delete_tree_removes_children(env, monkeypatch) -> None:
    """删除父任务必须整树移除（root + -iXXXX 子任务），否则列表平铺出孤儿。"""
    from secpt.core.job.resume import jobs_dir
    from secpt.core.job.store import JobStore

    _mk_record_status("tree-1", "succeeded")
    _mk_record_status("tree-1-i00aa", "succeeded")
    _mk_record_status("tree-1-i00bb", "failed")
    for jid in ("tree-1", "tree-1-i00aa", "tree-1-i00bb"):
        (jobs_dir() / jid).mkdir(parents=True, exist_ok=True)

    r = api.delete_job("tree-1")
    assert r.get("ok") is True and r.get("deleted") == 3
    for jid in ("tree-1", "tree-1-i00aa", "tree-1-i00bb"):
        assert JobStore().get_job(jid) is None


def test_delete_child_cancels_tree_root(env, monkeypatch) -> None:
    """删除子任务时按树根取消（协作式取消沿任务树传播），并整树删除。"""
    from secpt.core.job.resume import jobs_dir
    from secpt.core.job.store import JobStore

    _mk_record_status("troot", "running")
    _mk_record_status("troot-i00cc", "running")
    (jobs_dir() / "troot").mkdir(parents=True, exist_ok=True)
    (jobs_dir() / "troot-i00cc").mkdir(parents=True, exist_ok=True)
    called: list[str] = []
    monkeypatch.setattr(api, "cancel_task", lambda jid: called.append(jid) or {"ok": True})

    assert api.delete_job("troot-i00cc").get("ok") is True
    assert called == ["troot"]
    # delete_tree 以传入 id 为根：删子任务 → 子任务树；父任务本体由 cancel 标记收口，
    # 不在这次删除范围（用户点子任务的 ⋯ 菜单删的是该子任务所在树）
    assert JobStore().get_job("troot") is not None
    assert JobStore().get_job("troot-i00cc") is None


def test_stream_endpoint_sse_format(server) -> None:
    """SSE 流式接口：text/event-stream 响应，事件行带 id/data，收口发 bye。"""
    import json as _json
    import socket
    import time as _t

    host, port = server.split("//")[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=5)
    s.sendall(b"GET /api/jobs/sse-none/stream?after=0 HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(0.5)
    buf = s.recv(65536).decode("utf-8", errors="replace")
    s.close()
    assert "200" in buf.splitlines()[0]
    assert "text/event-stream" in buf
    # 无该任务目录 → 无事件、无 bye（保持连接），只可能有 keep-alive 注释
    assert "data:" not in buf or "bye" not in buf


def test_stream_endpoint_emits_events(server, env, monkeypatch) -> None:
    """有事件的任务：SSE 按 seq 顺序输出，after 游标生效。"""
    import socket
    import time as _t

    _mk_record_status("sse-job", "succeeded")
    _write_events(
        env,
        "sse-job",
        [{"type": "user_msg", "payload": {"kind": "new_task", "content": "hi"}},
         {"type": "status_change", "payload": {"status": "succeeded"}}],
    )
    host, port = server.split("//")[1].split(":")
    s = socket.create_connection((host, int(port)), timeout=5)
    s.sendall(b"GET /api/jobs/sse-job/stream?after=0 HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(0.8)
    s.settimeout(2)
    buf = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except (socket.timeout, OSError):
        pass
    s.close()
    text = buf.decode("utf-8", errors="replace")
    assert "id: 1" in text and "id: 2" in text
    assert "user_msg" in text and "event: bye" in text
