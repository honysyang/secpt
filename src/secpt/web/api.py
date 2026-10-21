"""Web API 层：payload 构造（纯函数优先，便于测试）+ 任务注册表。"""

from __future__ import annotations

import base64
import contextlib
import threading
import time
import uuid
from pathlib import Path

from secpt.challenge.attachments import sanitize_filename

_WEB_CONFIG_PATHNAME = "web/config.json"
_UPLOAD_PREFIX = "uploads"

# 进程内任务注册表：job_id → {"thread", "started_at", "mode"}
JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _var() -> Path:
    return Path(__import__("os").environ.get("SECPT_VAR", "var"))


# ---------------------------------------------------------------------------
# 配置（运行时覆盖项）
# ---------------------------------------------------------------------------

_CONFIGURABLE = {
    "model_primary": "SECPT_MODEL_PRIMARY",
    "model_fallback": "SECPT_MODEL_FALLBACK",
    "logistics_workers": "SECPT_LOGISTICS_WORKERS",
    "role_templates": "SECPT_ROLE_TEMPLATES",
    "orch_commander": "SECPT_ORCH_COMMANDER",
    "orch_delivery": "SECPT_ORCH_DELIVERY",
    "orch_logistics": "SECPT_ORCH_LOGISTICS",
    "mentor": "SECPT_MENTOR",
    "delegate": "SECPT_DELEGATE",
    "tool_pool": "SECPT_TOOL_POOL",
}


def _config_path() -> Path:
    return _var() / _WEB_CONFIG_PATHNAME


def load_config() -> dict:
    import json

    cfg: dict = {}
    with contextlib.suppress(Exception):
        cfg = json.loads(_config_path().read_text(encoding="utf-8"))
    return cfg if isinstance(cfg, dict) else {}


def apply_config_to_env(cfg: dict) -> None:
    """新任务启动前应用运行时覆盖（只写进程 env；不改用户的 .env 文件）。"""
    import os

    for key, env_name in _CONFIGURABLE.items():
        value = cfg.get(key)
        if value in (None, ""):
            continue
        os.environ[env_name] = str(value)


def config_payload() -> dict:
    import os

    out: dict = {"saved": load_config(), "effective": {}}
    for key, env_name in _CONFIGURABLE.items():
        out["effective"][key] = os.environ.get(env_name, "")
    return out


def save_config(cfg: dict) -> dict:
    import json
    import os

    clean = {k: str(v)[:200] for k, v in (cfg or {}).items() if k in _CONFIGURABLE and v != ""}
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_config_to_env(clean)
    return {
        "saved": clean,
        "effective": {k: os.environ.get(v, "") for k, v in _CONFIGURABLE.items()},
    }


# ---------------------------------------------------------------------------
# 各视图 payload（全部读既有单点事实源，不自建存储）
# ---------------------------------------------------------------------------


def overview_payload() -> dict:
    assets = findings = experiences = 0
    with contextlib.suppress(Exception):
        from secpt.intel.store import IntelStore

        assets = len(IntelStore(_var() / "intel.sqlite").assets(limit=1000))
    with contextlib.suppress(Exception):
        from secpt.findings.store import FindingStore

        findings = len(FindingStore(_var() / "findings.sqlite").list(limit=1000))
    with contextlib.suppress(Exception):
        from secpt.learning.store import ExperienceJournal

        experiences = len(ExperienceJournal().load())
    with contextlib.suppress(Exception):
        from secpt.core.job.store import JobStore

        jobs = len(JobStore().list_jobs(limit=1000))
    return {"assets": assets, "findings": findings, "experiences": experiences, "jobs": jobs}


def assets_payload(limit: int = 500) -> dict:
    from secpt.intel.store import IntelStore

    rows = IntelStore(_var() / "intel.sqlite").assets(limit=limit)
    return {"assets": rows, "total": len(rows)}


def findings_payload(limit: int = 300) -> dict:
    from secpt.findings.store import FindingStore

    rows = FindingStore(_var() / "findings.sqlite").list(limit=limit)
    return {
        "findings": [
            {
                "id": f.id,
                "title": f.title,
                "severity": f.severity,
                "status": f.status,
                "cwe": f.cwe,
                "target": f.target,
                "job_id": f.job_id,
                "confidence": round(float(f.confidence or 0), 2),
                "description": (f.description or "")[:400],
            }
            for f in rows
        ],
        "total": len(rows),
    }


def tools_payload() -> dict:
    """内置四工具 + 插件（YAML/Python）+ 打法技能库。"""
    from secpt.core.profile.loader import CORE_TOOLS
    from secpt.tools.plugin_loader import discover_plugins

    builtin = [{"name": t, "kind": "builtin", "description": ""} for t in sorted(CORE_TOOLS)]
    try:
        yaml_plugins, python_tools = discover_plugins()
    except Exception:
        yaml_plugins, python_tools = {}, {}
    plugins = [
        {"name": p.name, "kind": "plugin", "description": p.description[:200], "source": p.source}
        for p in yaml_plugins.values()
    ]
    for name, (schema, _fn) in python_tools.items():
        desc = ""
        with contextlib.suppress(Exception):
            desc = str((schema or {}).get("description") or "")
        plugins.append({"name": name, "kind": "plugin", "description": desc[:200], "source": "py"})
    skills: list[dict] = []
    try:
        import yaml

        tpath = Path(__file__).resolve().parents[1] / "knowledge" / "techniques.yaml"
        for item in yaml.safe_load(tpath.read_text(encoding="utf-8")) or []:
            if isinstance(item, dict) and item.get("name"):
                skills.append(
                    {
                        "name": str(item["name"]),
                        "summary": str(item.get("summary") or "")[:300],
                        "indicators": [str(i) for i in (item.get("indicators") or [])[:8]],
                    }
                )
    except Exception:
        skills = []
    return {"builtin": builtin, "plugins": plugins, "skills": skills}


def experiences_payload(limit: int = 200) -> dict:
    from secpt.learning.store import ExperienceJournal

    journal = ExperienceJournal()
    rows = journal.load()[-limit:]
    usage = {}
    with contextlib.suppress(Exception):
        usage = journal.load_usage()
    out = []
    for e in reversed(rows):
        d = e.to_dict()
        hit, total = usage.get(e.id, (0, 0))
        d["usage"] = {"hit": hit, "total": total}
        out.append(d)
    return {"experiences": out, "total": len(out)}


def jobs_payload(limit: int = 50) -> dict:
    from secpt.core.job.store import JobStore

    rows = JobStore().list_jobs(limit=limit)
    jobs = []
    for rec in rows:
        jobs.append(
            {
                "job_id": rec.job_id,
                "status": rec.status,
                "task_type": getattr(rec.spec, "task_type", ""),
                "objective": (getattr(rec.spec, "objective", "") or "")[:160],
                "created_at": getattr(rec, "created_at", None),
            }
        )
    return {"jobs": jobs}


# ---------------------------------------------------------------------------
# 事件流（seq 游标增量）
# ---------------------------------------------------------------------------


def _events_file(job_id: str) -> Path:
    from secpt.core.job.resume import jobs_dir

    return jobs_dir() / job_id / "events.jsonl"


def events_payload(job_id: str, after: int = 0, limit: int = 400) -> dict:
    """读取 events.jsonl 中 seq > after 的事件（执行轨迹的数据源）。

    轨迹分层在 前端 做：finding/flag/status → 对话里程碑；tool_call/tool_result →
    工具轨迹（配对）；intent 相关（frontier 聚合的 status_change/loop_phase）→ 智能体活动。
    """
    import json

    path = _events_file(job_id)
    events: list[dict] = []
    cursor = int(after or 0)
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            for line in f:
                with contextlib.suppress(Exception):
                    row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                seq = int(row.get("seq") or 0)
                cursor = max(cursor, seq)
                if seq <= int(after or 0):
                    continue
                if len(events) >= limit:
                    break
                row.setdefault("actor", row.get("agent_id") or row.get("job_id") or "agent")
                events.append(row)
    return {"events": events, "cursor": cursor, "has_more": len(events) >= limit}


# ---------------------------------------------------------------------------
# 工作台右栏：作战面板（任务池 / 假设树 / 战略文档 / 阶段性成果）
# ---------------------------------------------------------------------------


def workspace_payload(job_id: str) -> dict:
    out: dict = {
        "job_id": job_id,
        "intents": [],
        "hypotheses": [],
        "hypothesis_counts": {},
        "strategy": "",
        "flags": [],
        "findings": [],
        "delivery": None,
        "status": "",
    }
    with contextlib.suppress(Exception):
        from secpt.core.job.store import JobStore

        rec = JobStore().get_job(job_id)
        if rec is not None:
            out["status"] = rec.status
    # 任务池（frontier.sqlite，按任务隔离）
    with contextlib.suppress(Exception):
        from secpt.core.orchestrator.frontier import Frontier, frontier_db_path

        fr = Frontier(frontier_db_path(job_id))
        rows = fr.list_all(limit=100)
        out["intents"] = [
            {
                "id": r["id"],
                "title": r["title"],
                "status": r["status"],
                "lane": r.get("lane", "main"),
                "hypothesis": r.get("hypothesis", ""),
                "fail_reason": (r.get("fail_reason") or "")[:120],
            }
            for r in rows
        ]
    # 假设树
    with contextlib.suppress(Exception):
        from secpt.core.orchestrator.hypothesis import HypothesisTree, hypothesis_db_path

        tree = HypothesisTree(hypothesis_db_path(job_id))
        out["hypotheses"] = tree.digest()
        out["hypothesis_counts"] = tree.counts()
    # 战略文档（截断）
    with contextlib.suppress(Exception):
        from secpt.core.job.resume import jobs_dir

        spath = jobs_dir() / job_id / "strategy.md"
        if spath.is_file():
            out["strategy"] = spath.read_text(encoding="utf-8")[:1800]
    # 阶段性成果：flag / finding 事件
    with contextlib.suppress(Exception):
        payload = events_payload(job_id, after=0, limit=100000)
        for ev in payload["events"]:
            etype = ev.get("type")
            p = ev.get("payload") or {}
            if etype == "finding":
                out["findings"].append({"kind": p.get("kind"), "value": str(p.get("value"))[:160]})
    with contextlib.suppress(Exception):
        from secpt.core.orchestrator.flagsem import extract_real_flags

        raw = events_payload(job_id, after=0, limit=100000)
        texts = [
            str((ev.get("payload") or {}).get("content") or "")
            for ev in raw["events"]
            if ev.get("type") == "assistant"
        ]
        seen: set[str] = set()
        for t in texts:
            for fl in extract_real_flags(t):
                if fl not in seen:
                    seen.add(fl)
                    out["flags"].append(fl)
    with contextlib.suppress(Exception):
        from secpt.core.orchestrator.summary import RunSummary  # noqa: F401 契约存在性

        out["delivery"] = load_config().get(f"delivery:{job_id}")
    return out


# ---------------------------------------------------------------------------
# 任务提交（附件 base64 → var/uploads/<job>/；后台线程跑 dag.run_task）
# ---------------------------------------------------------------------------


def save_uploads(job_id: str, files: list[dict]) -> list[str]:
    """保存上传附件（base64），返回绝对路径列表（注入 TaskSpec.artifacts）。"""
    out_dir = _var() / _UPLOAD_PREFIX / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for f in files or []:
        name = sanitize_filename(str(f.get("name") or "attachment.bin")) or "attachment.bin"
        raw = f.get("data_b64") or ""
        with contextlib.suppress(Exception):
            data = base64.b64decode(raw, validate=False)
        if not data:
            continue
        dest = out_dir / name
        dest.write_bytes(data)
        paths.append(str(dest.resolve()))
    return paths


def _run_job(job_id: str, spec, llm) -> None:  # pragma: no cover - 线程体，集成验证
    try:
        import asyncio

        from secpt.core.job.store import JobStore
        from secpt.core.orchestrator.dag import run_task

        summary = asyncio.run(run_task(spec, store=JobStore(), llm=llm))
        # 交付判定等 extras 落配置文件供面板读取（框架阶段的最小持久化）
        extras = summary.get("delivery") if isinstance(summary, dict) else None
        if extras:
            cfg = load_config()
            cfg[f"delivery:{job_id}"] = extras
            save_config(cfg)
    except Exception as e:
        with contextlib.suppress(Exception):
            from secpt.core.job.event import EventSink
            from secpt.core.job.resume import jobs_dir

            EventSink(job_id, jobs_dir()).error(f"web task failed: {e}")
    finally:
        with _JOBS_LOCK, contextlib.suppress(Exception):
            from secpt.core.job.store import JobStore

            rec = JobStore().get_job(job_id)
            if rec is not None and rec.status in ("running", "pending"):
                rec.status = "completed"
                JobStore().upsert_job(rec)


def delete_job(job_id: str) -> dict:
    """删除任务：运行中先协作式取消，再从任务列表移除（目录留存可归档）。"""
    from secpt.core.job.store import JobStore

    jdir = _var() / "jobs"
    if (jdir / job_id).is_dir():
        rec = None
        with contextlib.suppress(Exception):
            rec = JobStore().get_job(job_id)
        if rec is not None and rec.status == "running":
            cancel_task(job_id)
    try:
        from secpt.core.job.store import JobStore

        JobStore().delete_job(job_id)
    except Exception as e:
        return {"error": f"删除失败: {e}"}
    return {"ok": True}


def cancel_task(job_id: str) -> dict:
    """关闭任务：整棵任务树协作式取消。

    三条通路（按优先级）：
    1. worker.pid 存在（submit/forever 池路径）→ SIGTERM（杀整树，子任务同进程）；
    2. cancel 标记文件（web 线程路径）→ 父 + 全部子 job 落标记，
       引擎每步检查到即优雅退出（status=cancelled，不丢 transcript）；
    3. JobRecord 状态置 cancelled（立即反馈；engine 退出后不再被覆盖）。
    """
    import os
    import signal as _signal

    from secpt.core.job.resume import jobs_dir
    from secpt.core.job.store import JobStore

    jdir = jobs_dir()
    if not (jdir / job_id).is_dir():
        return {"error": "任务不存在"}
    # 1) 池路径：worker 进程 SIGTERM
    pid_file = jdir / job_id / "worker.pid"
    killed = False
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, _signal.SIGTERM)
            killed = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    # 2) 协作式取消：定位树根（剥 -iXXXX/-r2/-rs 后缀），根 + 全树落标记——
    #    取消沿任务树传播，编排层后续派生的续段/重建引擎同样立即退出
    import re

    root = job_id
    while True:
        prev = root
        root = re.sub(r"-(?:i[0-9a-fA-F]{4,8}|r\d+|rs)$", "", root)
        if root == prev or not root:
            root = job_id
            break
    ids = [root] + [p.name for p in jdir.iterdir() if p.is_dir() and p.name.startswith(root + "-")]
    for jid in ids:
        with contextlib.suppress(OSError):
            (jdir / jid / "cancel").write_text("web-console")
    # 3) 状态立即置 cancelled（线程 finally 只覆盖 running/pending，不会改写）
    store = JobStore()
    with contextlib.suppress(Exception):
        rec = store.get_job(job_id)
        if rec is not None:
            rec.set_status("cancelled")
            store.upsert_job(rec)
    with contextlib.suppress(Exception):
        from secpt.core.job.event import EventSink

        EventSink(job_id, jdir).status_change("cancelled")
    return {"ok": True, "cancelled_jobs": ids, "signal": killed}


def inject_message(job_id: str, content: str) -> dict:
    """追问当前任务：用户消息转人工直投意图（source=direct）注入任务池。

    语义与 `secpt intent add` 一致：planner 看到 direct 未完成即暂停自动探索，
    执行体领取执行（人点单、AI 干活）。任务不存在/无编排 → 报错。
    """
    content = (content or "").strip()
    if not content:
        return {"error": "消息不能为空"}
    import re

    from secpt.core.orchestrator.frontier import Frontier, frontier_db_path

    # 子任务（-iXXXX 续段/-r2/-rs）没有自己的任务池——回退到父任务的池。
    # 子任务的产出由父任务消费，追问也应注入父池（规划让路在父编排生效）。
    candidates = [job_id]
    jid = job_id
    while True:
        prev = jid
        jid = re.sub(r"-(?:i[0-9a-fA-F]{4,8}|r\d+|rs)$", "", jid)
        if jid == prev or not jid:
            break
        candidates.append(jid)
    fr = None
    for cand in candidates:
        try:
            f = Frontier(frontier_db_path(cand))
            if f.total():
                fr = f
                break
        except Exception:
            continue
    if fr is None:
        return {"error": "任务不存在或未启用编排（仅编排型任务支持追问）"}
    iid = fr.add(
        f"人工直投：{content[:40]}",
        goal=f"【用户消息】{content[:800]}",
        done_criteria="按用户消息完成并给出结论",
        priority=10,
        depth=0,
        source="direct",
        lane="main",
    )
    with contextlib.suppress(Exception):
        from secpt.core.job.event import EventSink
        from secpt.core.job.resume import jobs_dir

        EventSink(job_id, jobs_dir()).write(
            "user_msg", {"kind": "direct", "content": content[:800]}
        )
    return {"ok": True, "intent_id": iid}


def create_task(
    objective: str,
    *,
    profile: str = "ctf",
    files: list[dict] | None = None,
    fake: bool = False,
    budget_steps: int = 0,
    budget_wall_s: float = 0.0,
) -> dict:
    """提交任务：附件落盘 → TaskSpec → 后台线程执行 → 立即返回 job_id。"""
    import os

    from secpt.core.job.models import TaskBudget, TaskSpec
    from secpt.core.job.store import JobStore

    objective = (objective or "").strip()
    if not objective:
        return {"error": "objective 不能为空"}
    apply_config_to_env(load_config())
    job_id = f"web-{uuid.uuid4().hex[:8]}"
    artifacts = save_uploads(job_id, files or [])
    spec = TaskSpec(
        job_id=job_id,
        task_type=profile,
        objective=objective,
        artifacts=artifacts,
        budget=TaskBudget(steps=int(budget_steps or 0), wall_s=float(budget_wall_s or 0.0)),
    )
    JobStore().upsert_job(
        __import__("secpt.core.job.models", fromlist=["JobRecord"]).JobRecord(
            job_id=job_id, spec=spec, status="running"
        )
    )
    llm = None
    if fake or os.environ.get("SECPT_FAKE_LLM") == "1":
        from secpt.core.job.fakellm import FakeLLM

        llm = FakeLLM(n_steps=max(1, int(budget_steps or 12)))
    # 用户下达内容落轨迹（对话面板右侧气泡；与 direct/steer 同一 user_msg 通道）
    with contextlib.suppress(Exception):
        from secpt.core.job.event import EventSink
        from secpt.core.job.resume import jobs_dir as _jobs_dir

        sink = EventSink(job_id, _jobs_dir())
        sink.write("user_msg", {"kind": "new_task", "content": objective[:800]})
        if artifacts:
            sink.write(
                "user_msg",
                {
                    "kind": "new_task",
                    "content": "📎 附件 "
                    + str(len(artifacts))
                    + " 个："
                    + "、".join(Path(a).name for a in artifacts[:5]),
                },
            )
    thread = threading.Thread(
        target=_run_job, args=(job_id, spec, llm), name=f"web-job-{job_id}", daemon=True
    )
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "thread": thread,
            "started_at": time.time(),
            "mode": "fake" if llm else "prod",
        }
    thread.start()
    return {"job_id": job_id, "artifacts": artifacts, "mode": "fake" if llm else "prod"}
