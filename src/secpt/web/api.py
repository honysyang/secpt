"""Web API 层：payload 构造（纯函数优先，便于测试）+ 任务注册表。"""

from __future__ import annotations

import base64
import contextlib
import sys
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
    # v10 轮转归档回读：游标早于在线文件首 seq 时，从 archive/*.jsonl.gz 补
    with contextlib.suppress(Exception):
        from secpt.core.sustain.compress import read_archived_events

        _wm = path.parent / "archive" / "watermark.json"
        if _wm.is_file():
            import json as _json

            _min_kept = int(_json.loads(_wm.read_text(encoding="utf-8")).get("min_kept_seq") or 0)
            if 0 < int(after or 0) < _min_kept:
                for row in read_archived_events(job_id, int(after), _min_kept):
                    events.append(row)
                    cursor = max(cursor, int(row.get("seq") or 0))
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


def job_file_payload(job_id: str, *, sub: str = "", path: str = "") -> dict:
    """读取意图子任务工作区文件（对话面板文件卡展开/复制用）。

    事件流里的 tool_call.args 被截断（500 字符），前端无法据此还原写文件
    的路径与正文，只能回到磁盘读。约束：只允许父 job 自身或其子 job
    （<job_id>-iXXXX/-r2/-rs）的 ws；解析后路径必须仍在 ws 内；上限 512KB。
    """
    jid = str(job_id or "").strip()
    sid = str(sub or "").strip() or jid
    rel = str(path or "").strip()
    if not jid or not rel:
        return {"error": "缺少参数（sub/path）"}
    if not (sid == jid or sid.startswith(jid + "-")):
        return {"error": "子任务不属于该任务"}
    base = (_var() / "jobs" / sid / "ws").resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return {"error": "路径越界"}
    if not target.is_file():
        return {"error": "文件不存在（可能已被压缩/清理）"}
    size = target.stat().st_size
    if size > 512 * 1024:
        return {"error": f"文件过大（{size}B），请在 CLI 查看"}
    return {
        "ok": True,
        "sub": sid,
        "path": rel,
        "bytes": size,
        "content": target.read_bytes().decode("utf-8", errors="replace"),
    }


def workspace_payload(job_id: str) -> dict:
    out: dict = {
        "job_id": job_id,
        "intents": [],
        "hypotheses": [],
        "hypothesis_counts": {},
        "strategy": "",
        "focus": "",
        "goal": None,
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
                "source": r.get("source", ""),
                "priority": int(r.get("priority") or 5),
                "attempts": int(r.get("attempts") or 1),
                "created_at": float(r.get("created_at") or 0),
                "updated_at": float(r.get("updated_at") or 0),
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
    # 战略文档（截断）+ 主攻方向摘录（作战地图头部：当前打哪）
    with contextlib.suppress(Exception):
        from secpt.core.job.resume import jobs_dir

        spath = jobs_dir() / job_id / "strategy.md"
        if spath.is_file():
            out["strategy"] = spath.read_text(encoding="utf-8")[:1800]
    with contextlib.suppress(Exception):
        from secpt.core.orchestrator.frontier import _strategy_focus

        out["focus"] = _strategy_focus(jobs_dir() / job_id / "strategy.md")
    # 结构化目标（指挥官首轮理解产物；resume 复用同一份）
    with contextlib.suppress(Exception):
        from secpt.core.orchestrator.goals import load_goal

        _g = load_goal(job_id)
        if _g is not None:
            out["goal"] = {
                "objective": _g.objective,
                "asset_scope": list(_g.asset_scope or []),
                "attack_steps": list(_g.attack_steps or []),
                "restatement": _g.restatement,  # 接战单：目标重述（确认卡渲染）
                "success_criteria": list(_g.success_criteria or []),
                "constraints": list(_g.constraints or []),
            }
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
        # delivery.json（job 目录）优先；旧版 web 配置键兜底
        import json

        from secpt.core.orchestrator.summary import RunSummary  # noqa: F401 契约存在性

        _dj = _var() / "jobs" / job_id / "delivery.json"
        if _dj.is_file():
            out["delivery"] = json.loads(_dj.read_text(encoding="utf-8"))
        else:
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


def _run_job(
    job_id: str, spec, llm, confirm_event=None
) -> None:  # pragma: no cover - 线程体，集成验证
    _ok = False
    try:
        import asyncio

        from secpt.core.job.store import JobStore
        from secpt.core.orchestrator.dag import run_task

        summary = asyncio.run(
            run_task(spec, store=JobStore(), llm=llm, confirm_event=confirm_event)
        )
        _ok = True
        # 交付判定落 job 目录（delivery.json）供面板读取——不能走 save_config：
        # 那只放行 _CONFIGURABLE 白名单键，delivery:* 会被静默丢弃（真实运行暴露）
        extras = summary.get("delivery") if isinstance(summary, dict) else None
        if extras:
            with contextlib.suppress(Exception):
                from secpt.core.job.resume import jobs_dir

                _dp = jobs_dir() / job_id / "delivery.json"
                _dp.parent.mkdir(parents=True, exist_ok=True)
                import json

                _dp.write_text(json.dumps(extras, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[web] job {job_id} 运行异常: {type(e).__name__}: {e}", file=sys.stderr)
        with contextlib.suppress(Exception):
            from secpt.core.job.event import EventSink
            from secpt.core.job.resume import jobs_dir

            EventSink(job_id, jobs_dir()).error(f"web task failed: {e}")
    finally:
        # 终态必须用 JOB_STATUSES 合法值（"completed" 不在其中，upsert 会
        # ValueError 并被下面的 suppress 吞掉——任务永远卡 running，真实运行暴露）
        with _JOBS_LOCK, contextlib.suppress(Exception):
            from secpt.core.job.store import JobStore

            rec = JobStore().get_job(job_id)
            if rec is not None and rec.status in ("running", "pending"):
                rec.status = "succeeded" if _ok else "failed"
                JobStore().upsert_job(rec)


def confirm_task(job_id: str) -> dict:
    """接战确认：放行挂起的编排（首轮受理已产《接战单》，status=awaiting_confirm）。"""
    with _JOBS_LOCK:
        rec = JOBS.get(job_id)
        ev = (rec or {}).get("confirm_event")
    if ev is None:
        return {"error": "该任务不在待确认状态（可能已确认/自动下发/已完成）"}
    ev.set()
    with _JOBS_LOCK:
        rec["confirm_event"] = None  # 一次性：重复确认报错（幂等防双击）
    with contextlib.suppress(Exception):
        from secpt.core.job.event import EventSink
        from secpt.core.job.resume import jobs_dir

        EventSink(job_id, jobs_dir()).write(
            "loop_phase", {"phase": "confirm_clicked", "reason": "人工在 Web 确认接战单"}
        )
    return {"ok": True}


def job_active(job_id: str) -> bool:
    """该任务的编排线程是否仍在运行（JOBS 注册表 + 线程存活）。"""
    with _JOBS_LOCK:
        rec = JOBS.get(job_id)
    return bool(rec and rec.get("thread") and rec["thread"].is_alive())


def resume_task(job_id: str) -> dict:
    """对话续接：同一作战地图再跑一轮编排。

    前端对话中发布任务的承载体——任务收敛后用户继续发消息，direct 意图已入池，
    这里用原 job_id 重启编排：frontier/hypothesis/goal.json/role.json 全部沿用
    （v5/v6 的 resume 语义），不新建任务、对话历史与作战地图保持同一张。
    """
    import os

    from secpt.core.job.store import JobStore

    store = JobStore()
    rec = store.get_job(job_id)
    if rec is None or getattr(rec, "spec", None) is None:
        return {"error": "任务不存在或无 spec，无法自动续接"}
    if job_active(job_id):
        return {"ok": True, "resumed": False}
    rec.status = "running"
    store.upsert_job(rec)
    llm = None
    if os.environ.get("SECPT_FAKE_LLM") == "1":
        from secpt.core.job.fakellm import FakeLLM

        llm = FakeLLM(n_steps=12)
    thread = threading.Thread(
        target=_run_job, args=(job_id, rec.spec, llm), name=f"web-job-{job_id}", daemon=True
    )
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "thread": thread,
            "started_at": time.time(),
            "mode": "fake" if llm else "prod",
            "resume": True,
        }
    thread.start()
    with contextlib.suppress(Exception):
        from secpt.core.job.event import EventSink
        from secpt.core.job.resume import jobs_dir

        EventSink(job_id, jobs_dir()).write(
            "loop_phase", {"phase": "resume", "reason": "对话追问续接编排（同一作战地图）"}
        )
    return {"ok": True, "resumed": True}


def delete_job(job_id: str) -> dict:
    """删除任务：运行中先协作式取消，再从任务列表移除（目录留存可归档）。"""
    from secpt.core.job.store import JobStore

    jdir = _var() / "jobs"
    if (jdir / job_id).is_dir():
        rec = None
        with contextlib.suppress(Exception):
            rec = JobStore().get_job(job_id)
        # 非终态都要先取消：queued/pending 的任务删掉后线程仍会启动，
        # 会派生出一堆失去父任务的孤儿子任务（web-8d3c345d 实证）
        _ACTIVE = {"queued", "pending", "running", "awaiting_confirm", "paused"}
        if rec is not None and rec.status in _ACTIVE:
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
    """对话中发布任务：用户消息转人工直投意图（source=direct）注入任务池。

    语义与 `secpt intent add` 一致：planner 看到 direct 未完成即暂停自动探索，
    执行体领取执行（人点单、AI 干活）。对话即任务流——一次对话可先后发布
    多个任务（每条消息一条 direct 意图），共用同一张作战地图。

    任务已收敛时自动续接编排（resume_task）：同 job_id 重启编排，
    frontier/hypothesis/goal/role 全部沿用，不新建任务、不换对话。
    """
    content = (content or "").strip()
    if not content:
        return {"error": "消息不能为空"}
    import re

    from secpt.core.job.store import JobStore as _JS

    with contextlib.suppress(Exception):
        _rec0 = _JS().get_job(job_id)
        if _rec0 is not None and _rec0.status == "awaiting_confirm":
            return {"error": "接战单待确认：请先在作战地图确认开战（或取消任务后重发新任务）"}
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
    owner = job_id
    for cand in candidates:
        try:
            f = Frontier(frontier_db_path(cand))
            if f.total():
                fr = f
                owner = cand
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
    resumed = False
    if not job_active(owner):
        # 池归父任务但注册表可能只认得父 id；owner 即持有该池的编排任务
        res = resume_task(owner)
        resumed = bool(res.get("resumed"))
    return {"ok": True, "intent_id": iid, "resumed": resumed}


def create_task(
    objective: str,
    *,
    profile: str = "auto",  # 缺省 auto：角色/场景/经验由指挥官首轮选定并赋予操作员
    files: list[dict] | None = None,
    fake: bool = False,
    budget_steps: int = 0,
    budget_wall_s: float = 0.0,
    auto_dispatch: bool = True,  # False=接战确认闸：CDR 先产《接战单》，人工确认后才编成派单
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
    _confirm_event = None
    if not auto_dispatch:
        _confirm_event = threading.Event()  # 接战确认闸：挂起编排，等 /confirm 放行
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, spec, llm, _confirm_event),
        name=f"web-job-{job_id}",
        daemon=True,
    )
    with _JOBS_LOCK:
        JOBS[job_id] = {
            "confirm_event": _confirm_event,
            "thread": thread,
            "started_at": time.time(),
            "mode": "fake" if llm else "prod",
        }
    thread.start()
    return {"job_id": job_id, "artifacts": artifacts, "mode": "fake" if llm else "prod"}
