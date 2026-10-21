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

    def fake_run(job_id, spec, llm):
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

    def fake_run(job_id, spec, llm):
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

    def fake_run(job_id, spec, llm):
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
