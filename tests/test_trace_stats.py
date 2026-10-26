"""S13 验收：只读观测层（trace / stats / context.assembled）。

- trace 对同一 mission 渲染两次逐字节相等，且产生零新事件
  （跑前跑后 events.jsonl 行数与 sha256 不变）；
- stats 对 3 个不同终态的 mission 一行一个，聚合数正确；
- context.assembled 事件不含任何目标字符串（用带 10.0.0.5 的任务测）。
"""
from __future__ import annotations

import hashlib
import json
import socket

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.infra.llm import MockLLM
from pa.models import AcceptanceItem, Budget, MissionSpec
from pa.report import stats as stats_mod
from pa.report import trace as trace_mod
from pa.runtime import run

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


@pytest.fixture
def listener() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


def _freeze():
    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts",
                                [ROOT / "skills"])


def _spec(port: int, *, steps: int = 12, evidence_: str = "open",
          goal: str | None = None) -> MissionSpec:
    return MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[AcceptanceItem(id="acc-1", item="端口开放被证实",
                                   evidence=evidence_, target="127.0.0.1")],
        budget=Budget(steps=steps, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{port}"],
    )


def _planner_script(port: int, evidence_: str, goal: str | None = None) -> str:
    return json.dumps(
        {"tasks": [{"type": "recon",
                    "goal": goal or f"probe 127.0.0.1:{port}",
                    "success_criteria": [{"evidence": evidence_}],
                    "scope_slice": {}, "allowed_tools": ["probe"],
                    "budget": {"steps": 4}}],
         "current_strategy": {}, "rationale": "r"}, ensure_ascii=False)


def _worker_script(port: int) -> str:
    return json.dumps(
        {"kind": "call_tool", "tool": "probe",
         "args": {"target": "127.0.0.1", "port": port},
         "expect": "端口开放"}, ensure_ascii=False)


def _finish_script() -> str:
    return json.dumps({"kind": "finish", "summary": "声称完成"},
                      ensure_ascii=False)


def _run(tmp_mission, name, port, *, steps=12, evidence_="open",
         worker=None, goal=None):
    d = tmp_mission / name
    llm = MockLLM({"planner": [_planner_script(port, evidence_, goal)],
                   "worker": [worker or _worker_script(port)]})
    state = run(d, _spec(port, steps=steps, evidence_=evidence_),
                llm=llm, assets=_freeze())
    return d, state


def _ledger(d):
    raw = (d / "events.jsonl").read_bytes()
    return raw, hashlib.sha256(raw).hexdigest(), raw.count(b"\n")


# ─────────────────────────── trace ───────────────────────────

def test_trace_byte_stable_and_readonly(tmp_mission, listener):
    d, _ = _run(tmp_mission, "m", listener)
    raw0, sha0, lines0 = _ledger(d)

    tr = trace_mod.build(d)
    a_txt = trace_mod.render_text(tr, root=d)
    b_txt = trace_mod.render_text(trace_mod.build(d), root=d)
    assert a_txt == b_txt                            # 两次渲染逐字节相等
    a_json = trace_mod.render_json(trace_mod.build(d))
    b_json = trace_mod.render_json(trace_mod.build(d))
    assert a_json == b_json

    # 过滤参数也确定性
    f1 = trace_mod.render_json(trace_mod.build(d, step=1, task="t-0001"))
    f2 = trace_mod.render_json(trace_mod.build(d, step=1, task="t-0001"))
    assert f1 == f2

    # 只读：跑前跑后行数与 sha 不变，零新事件
    raw1, sha1, lines1 = _ledger(d)
    assert raw0 == raw1 and sha0 == sha1 and lines0 == lines1


def test_trace_sections_and_payload_field_names(tmp_mission, listener):
    d, _ = _run(tmp_mission, "m", listener)
    tr = trace_mod.build(d)
    # 四段齐全
    for key in ("plan", "timeline", "failures", "clues"):
        assert key in tr
    assert tr["plan"]["mission"]["objective"] == "探测本地监听端口"
    assert tr["plan"]["tasks"][0]["id"] == "t-0001"
    assert tr["finish"]["reason"] == "completed"
    # 时间线按 (step, task, seq) 排序
    keys = [(e["step"], e["task"], e["seq"]) for e in tr["timeline"]]
    assert keys == sorted(keys)
    assert keys, "时间线不应为空"
    # --json 字段名与事件 payload 一致：action.finished 的 exit/evidence/cached
    fin = [e for e in tr["timeline"] if e["type"] == "action.finished"]
    assert fin and {"exit", "evidence", "cached", "fp"} <= set(fin[0])
    # step 过滤
    tr1 = trace_mod.build(d, step=1)
    assert tr1["timeline"] and all(e["step"] == 1 for e in tr1["timeline"])
    # task 过滤
    trt = trace_mod.build(d, task="t-0001")
    assert all(e["task"] == "t-0001" for e in trt["timeline"])


def test_trace_text_has_four_sections(tmp_mission, listener, capsys):
    from pa.cli import main
    d, _ = _run(tmp_mission, "m", listener)
    assert main(["trace", str(d)]) == 0
    out = capsys.readouterr().out
    for sec in ("== 计划 ==", "== 时间线 ==", "== 失败与重试 ==",
                "== 线索与裁决 =="):
        assert sec in out
    assert main(["trace", str(d), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["finish"]["reason"] == "completed"


# ─────────────────────────── stats ───────────────────────────

def test_stats_three_terminals_one_row_each(tmp_mission, listener):
    d1, s1 = _run(tmp_mission, "m1", listener)                       # completed
    d2, s2 = _run(tmp_mission, "m2", listener, steps=2,
                  evidence_="never-ever-met")                        # budget_steps
    bad = json.dumps({"kind": "call_tool", "tool": "probe",
                      "args": {"target": "10.255.255.1", "port": 80},
                      "expect": "x"}, ensure_ascii=False)
    d3, s3 = _run(tmp_mission, "m3", listener, evidence_="never-ever-met",
                  worker=bad)                                        # plateau
    assert (s1.finish_reason, s2.finish_reason, s3.finish_reason) == \
        ("completed", "budget_steps", "plateau")

    rows = [stats_mod.collect(d) for d in (d1, d2, d3)]
    assert [r["terminal"] for r in rows] == ["completed", "budget_steps",
                                             "plateau"]
    assert rows[0]["tasks"].get("done") == 1
    assert rows[1]["steps"] == 2
    assert rows[2]["peak_no_progress"] >= 1   # P1-1：排空即收口，峰值=空转轮数
    assert rows[2]["authz"] == 1                     # out_of_scope 计入 AuthZ

    text = stats_mod.render(rows)
    # 一行一个 mission（表体恰好 3 行：表头分隔线下、## 聚合 之上）
    table = text.split("## 聚合")[0]
    body = [l for l in table.splitlines() if l and not l.startswith(("MISSION", "-", "#"))]
    assert len(body) == 3
    assert "调参建议" in text

    agg = stats_mod.aggregate(rows)
    assert agg["missions"] == 3
    assert agg["vs_events"] == 0
    assert agg["vs_task_rate"] == 0.0
    assert agg["authz_ratio"] == 1.0                 # 唯一一次阻塞就是 AuthZ


def test_stats_suggestions_fire_on_threshold_crossing(tmp_mission, listener):
    # worker 立即 finish、判据永不满足 → virtual_success ×3（打满尝试）
    d, s = _run(tmp_mission, "vs", listener, evidence_="never-ever-met",
                worker=_finish_script())
    assert s.finish_reason == "plateau"
    rows = [stats_mod.collect(d)]
    agg = stats_mod.aggregate(rows)
    assert agg["vs_events"] == 3                     # 按事件计 3 次
    assert agg["vs_task_rate"] == 1.0                # 按任务去重 1 个任务
    tips = stats_mod.suggest(agg)
    assert any("vs_task_rate" in t for t in tips)
    assert any("尝试" in t for t in tips)


# ─────────────────────────── context.assembled ───────────────────────────

def test_context_assembled_has_no_target_leak(tmp_mission, listener):
    goal = "先确认 10.0.0.5 的可达性，再探 127.0.0.1"
    d, _ = _run(tmp_mission, "leak", listener, goal=goal)
    lines = (d / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ctx = [json.loads(l) for l in lines
           if json.loads(l)["type"] == "context.assembled"]
    assert ctx, "应有 context.assembled 事件"
    for ev in ctx:
        assert "10.0.0.5" not in json.dumps(ev["payload"], ensure_ascii=False)
        # 只记尺寸与占比：段名 + 数字
        for sec in ev["payload"]["sections"].values():
            assert set(sec) == {"chars", "pct"}
    # 对照：目标字符串确实进了账本（task.created），只是观测事件不带
    assert any("10.0.0.5" in json.loads(l)["payload"].get("task", {}).get("goal", "")
               for l in lines if json.loads(l)["type"] == "task.created")
    # stats 能消费：prompt 斜率非负且 worker 步数分布非空
    row = stats_mod.collect(d)
    assert row["worker_step_dist"]
    assert row["prompt_slope"] >= 0
