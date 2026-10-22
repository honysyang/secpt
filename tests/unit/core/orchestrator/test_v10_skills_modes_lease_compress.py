"""v10 改造单测：技能动态发现 / 模式收敛裁决 / 全局目标租约 / 产物自动压缩（保 seq）/ 经验回灌。"""

from __future__ import annotations

import json

from secpt.core.job.event import EventSink
from secpt.core.job.resume import jobs_dir
from secpt.core.orchestrator import global_lease
from secpt.core.orchestrator.frontier import (
    Frontier,
    _parent_event,
    build_intent_objective,
    frontier_db_path,
)
from secpt.core.orchestrator.modes import resolve_orch_mode
from secpt.core.skills import discover_skills, load_skill, valid_skill
from secpt.core.sustain.compress import compress_job_dir, read_archived_events

# ---------------- 技能动态发现 ----------------


def test_discover_repo_skills():
    names = [s.name for s in discover_skills()]
    assert "web-enumerator" in names
    assert "pwn-exploit" in names
    sk = load_skill("web-enumerator")
    assert sk is not None and sk.body and sk.description


def test_valid_skill_rejects_traversal_and_unknown():
    assert valid_skill("../etc") is False
    assert valid_skill("a/b") is False
    assert valid_skill("no-such-skill") is False
    assert valid_skill("web-enumerator") is True


def test_skills_dirs_override_by_later_dir(tmp_path, monkeypatch):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    for d in (d1, d2):
        (d / "shared-skill").mkdir(parents=True)
        (d / "shared-skill" / "SKILL.md").write_text(
            "# shared-skill\nBODY-" + d.name, encoding="utf-8"
        )
    monkeypatch.setenv("SECPT_SKILLS_DIRS", f"{d1}:{d2}")
    sk = load_skill("shared-skill")
    assert sk is not None and "BODY-d2" in sk.body  # 后写目录覆盖先写


# ---------------- 模式收敛 ----------------


def test_mode_default_is_commander_clean():
    m = resolve_orch_mode(env={})
    assert m.mode == "commander" and not m.warnings


def test_mode_stages_wins_with_conflict_warning():
    m = resolve_orch_mode(stages=["a"], env={"SECPT_ORCH_COMMANDER": "0"})
    assert m.mode == "stages" and m.warnings


def test_mode_role_plan_is_team_legacy():
    m = resolve_orch_mode(role_plan=True, env={})
    assert m.mode == "team-legacy" and m.warnings


def test_mode_legacy_deprecation():
    m = resolve_orch_mode(env={"SECPT_ORCH_COMMANDER": "0"})
    assert m.mode == "legacy"
    assert any("弃用" in w for w in m.warnings)


def test_mode_role_templates_warns_but_stays_commander():
    m = resolve_orch_mode(env={"SECPT_ROLE_TEMPLATES": "1"})
    assert m.mode == "commander" and m.warnings


# ---------------- 全局目标租约 ----------------


def test_lease_acquire_contend_and_release(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    ok, _ = global_lease.acquire("http://t.local", "jobA")
    assert ok
    ok2, note2 = global_lease.acquire("http://t.local", "jobA")  # 同 job 重入
    assert ok2 and note2 == "self"
    ok3, note3 = global_lease.acquire("http://t.local", "jobB")
    assert not ok3 and "jobA" in note3
    h = global_lease.holder("http://t.local")
    assert h and h.get("job") == "jobA"

    global_lease.release("http://t.local", "jobB")  # 非持有者释放无效
    assert global_lease.holder("http://t.local")
    n = global_lease.release_all("jobA")
    assert n >= 1
    assert global_lease.holder("http://t.local") is None
    ok4, _ = global_lease.acquire("http://t.local", "jobB")
    assert ok4


def test_lease_ttl_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setattr(global_lease, "_TTL_S", 60.0)  # 模块级常量：导入时已绑定 env
    global_lease.acquire("http://ttl.local", "jobA")
    # 把租约文件 ts 拨回 TTL 之前 → 过期：持有者可被新任务重抢
    f = next((tmp_path / "leases").glob("*.json"))
    data = json.loads(f.read_text(encoding="utf-8"))
    data["ts"] = data["ts"] - 3600
    f.write_text(json.dumps(data), encoding="utf-8")
    assert global_lease.holder("http://ttl.local") is None  # 过期即清理
    ok, _ = global_lease.acquire("http://ttl.local", "jobB")
    assert ok and global_lease.holder("http://ttl.local")["job"] == "jobB"


# ---------------- 产物自动压缩（轮转归档保 seq） ----------------


def _seed_events(base: str, n: int = 600) -> None:
    d = jobs_dir() / base
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "seq": i,
                "job_id": base,
                "ts": 1.0,
                "round": 0,
                "type": "assistant",
                "payload": {"content": str(i)},
            },
            ensure_ascii=False,
        )
        for i in range(1, n + 1)
    ]
    (d / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_compress_rotation_keeps_tail_and_watermark(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    base = "cmp-rot"
    _seed_events(base, 600)
    stats = compress_job_dir(base, keep_events=100)
    d = jobs_dir() / base
    wm = json.loads((d / "archive" / "watermark.json").read_text(encoding="utf-8"))
    assert wm["min_kept_seq"] == 501 and wm["archived_upto"] == 500
    online = [
        json.loads(ln) for ln in (d / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert online[0]["payload"]["phase"] == "archive" and online[0]["seq"] == 500  # 桥接标记
    kept = [e["seq"] for e in online[1:]]
    assert kept == list(range(501, 601))
    assert any(stats.values())


def test_archive_read_range(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    base = "cmp-read"
    _seed_events(base, 600)
    compress_job_dir(base, keep_events=100)
    rows = read_archived_events(base, 10, 60)
    assert [r["seq"] for r in rows] == list(range(11, 60))  # 开区间 (after, upto_seq)


def test_seq_continues_after_rotation(tmp_path, monkeypatch):
    """轮转后行数(101) < 尾 seq(600)：'行数+1' 会回退撞号，三写入方必须看尾行。"""
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    base = "cmp-seq"
    _seed_events(base, 600)
    compress_job_dir(base, keep_events=100)
    _parent_event(base, "intent_event", {"status": "done"})
    EventSink(base, jobs_dir()).write("assistant", {"content": "post", "tool_calls": []})
    seqs = [
        json.loads(ln)["seq"]
        for ln in (jobs_dir() / base / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert seqs[-2:] == [601, 602]
    assert len(set(seqs)) == len(seqs)


def test_events_payload_falls_back_to_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    base = "cmp-api"
    _seed_events(base, 600)
    compress_job_dir(base, keep_events=100)
    from secpt.web import api

    payload = api.events_payload(base, after=10, limit=1000)
    got = {e["seq"] for e in payload["events"]}
    assert 11 in got and 600 in got  # 归档段 + 在线段都拿到
    assert payload["cursor"] >= 600


# ---------------- 意图级技能挂载 → 操作员目标注入 ----------------


def test_intent_skills_injected_into_objective(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    fr = Frontier(frontier_db_path("skl-job"))
    fr.add("web 侦察", goal="识别技术栈", done_criteria="有结论", skills="web-enumerator,no-such")
    intent = fr.claim_next()
    assert intent is not None and intent.skills == "web-enumerator,no-such"
    obj = build_intent_objective("拿下靶机", intent, [])
    assert "技能包" in obj and "web-enumerator" in obj
    assert "no-such" not in obj  # 未知技能静默忽略


# ---------------- 经验命中率回灌指挥官简报 ----------------


def test_experience_hints_scored_by_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    from secpt.core.orchestrator.commander import _experience_hints
    from secpt.learning.store import ExperienceJournal, LearnedExperience

    journal = ExperienceJournal()
    journal.append(
        LearnedExperience(
            title="目录爆破找后台",
            technique="web",
            what_worked="ffuf 扫 /admin 命中",
            outcome="success",
        )
    )
    eid = journal.load()[0].id
    for _ in range(3):
        journal.record_usage(eid, True)
    hints = _experience_hints("web recon admin panel")
    assert "目录爆破找后台" in hints and "命中 3/3" in hints
