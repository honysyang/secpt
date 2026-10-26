"""secpt trace：只读观测层（S13）。从事件账本还原一次 mission 的执行轨迹。

硬规矩（验收）：
- 只读 events.jsonl（StoreView.events），不调任何写接口、不产生事件——
  跑前跑后 events.jsonl 的行数与 sha256 不变；
- 确定性：同一份账本渲染两次逐字节相等——不渲染 Event.ts，排序固定
  （时间线按 (step, task, action 序号)）；
- --json 输出结构化版本，字段名与事件 payload 一致，便于二次处理。

四段：计划 / 时间线 / 失败与重试 / 线索与裁决。step 归属：第 N 个
budget.charged 之前的所有事件属于第 N 个超级步（引导阶段的事件属于
第 1 步之前显示为 step 1——首个调度轮即第 1 步）。
"""
from __future__ import annotations

import json
from pathlib import Path

from pa.adjudicate import finding_id
from pa.errors import SecptError
from pa.models import FindingProposal
from pa.store.view import StoreView

_NO_TASK = "-"


def build(root, *, step: int | None = None, task: str | None = None) -> dict:
    """读账本，产出结构化轨迹 dict（filter 只影响展示段，不改原始数据）。"""
    root = Path(root)
    events = list(StoreView(root).events())
    if not events:
        raise SecptError(f"无事件账本: {root}")

    plan: dict = {"mission": None, "plans": [], "tasks": []}
    timeline: list[dict] = []
    failures: list[dict] = []
    clues: list[dict] = []
    findings: list[dict] = []
    adjudications: list[dict] = []
    fin: dict = {}

    cur_step = 1
    seqs: dict[tuple[int, str], int] = {}
    fp_tool: dict[str, str] = {}            # action.started 带 tool，finished 不带，按 fp 配对

    def seq_of(s: int, tk: str) -> int:
        key = (s, tk)
        seqs[key] = seqs.get(key, 0) + 1
        return seqs[key]

    def tl(t: str, tk: str, payload: dict) -> None:
        timeline.append({"step": cur_step, "task": tk, "seq": seq_of(cur_step, tk),
                         "type": t, **payload})

    for ev in events:
        t, p = ev.type, ev.payload
        if t == "budget.charged":
            cur_step += 1
        elif t == "mission.created":
            plan["mission"] = dict(p)
        elif t in ("plan.created", "plan.revised"):
            plan["plans"].append(dict(p))
        elif t == "task.created":
            tk = p["task"]
            plan["tasks"].append({
                "id": tk["id"], "type": tk["type"], "goal": tk["goal"],
                "deps": list(tk.get("deps", [])),
                "allowed_tools": list(tk.get("allowed_tools", [])),
                "success_criteria": len(tk.get("success_criteria", [])),
            })
        elif t == "task.dispatched":
            tl(t, p.get("task_id", _NO_TASK), {"attempt": p.get("attempt")})
        elif t == "task.state_changed":
            tk = p.get("task_id", _NO_TASK)
            entry = {"from": p.get("from"), "to": p.get("to"),
                     "reason": p.get("reason"), "attempt": p.get("attempt")}
            tl(t, tk, entry)
            if p.get("to") in ("failed", "blocked"):
                failures.append({"step": cur_step, "task": tk, **entry})
        elif t == "restart.decided":
            entry = {"attempt": p.get("attempt")}
            tl(t, p.get("task_id", _NO_TASK), entry)
            failures.append({"step": cur_step, "task": p.get("task_id"),
                             "kind": "restart", **entry})
        elif t == "action.started":
            fp = str(p.get("fp", ""))
            fp_tool[fp] = str(p.get("tool", "?"))
            tl(t, p.get("task_id", _NO_TASK),
               {"tool": p.get("tool"), "fp": fp[:12]})
        elif t == "action.finished":
            fp = str(p.get("fp", ""))
            # P2-1：payload 自带 tool，直接采用；fp 配对只作旧账本回退
            tool = p.get("tool") or fp_tool.get(fp, "")
            entry = {"tool": tool, "fp": fp[:12], "exit": p.get("exit"),
                     "evidence": p.get("evidence"), "cached": p.get("cached")}
            tl(t, p.get("task_id", _NO_TASK), entry)
            if not p.get("cached") and int(p.get("exit", 0)) != 0:
                failures.append({"step": cur_step,
                                 "task": p.get("task_id", _NO_TASK),
                                 "kind": "action", **entry})
        elif t == "action.blocked":
            entry = {"tool": p.get("tool"), "reason": p.get("reason")}
            tl(t, p.get("task_id", _NO_TASK), entry)
            failures.append({"step": cur_step,
                             "task": p.get("task_id", _NO_TASK),
                             "kind": "blocked", **entry})
        elif t == "worker.returned":
            tk = p.get("task_id", _NO_TASK)
            entry = {"status": p.get("status"), "failure": p.get("failure")}
            tl(t, tk, entry)
            if p.get("failure") is not None or p.get("status") in (
                    "failed", "blocked", "needs_help"):
                failures.append({"step": cur_step, "task": tk, **entry})
        elif t == "mentor.intervened":
            tl(t, _NO_TASK, {"level": p.get("level"), "issue": p.get("issue")})
        elif t == "budget.warning":
            tl(t, _NO_TASK, {"spent_tokens": p.get("spent_tokens"),
                             "budget_tokens": p.get("budget_tokens")})
        elif t == "context.assembled":
            tl(t, p.get("task_id", _NO_TASK),
               {"total_chars": p.get("total_chars")})
        elif t == "clue.added":
            c = p["clue"]
            value = c.get("value")
            if c.get("kind") == "cred":
                value = str(value)[:4] + "…"          # 观测层同样脱敏
            clues.append({"kind": c.get("kind"), "key": c.get("key"),
                          "value": value,
                          "task": c.get("source_task")})
        elif t == "finding.proposed":
            f = FindingProposal.model_validate(p["finding"])
            findings.append({"finding_id": finding_id(f), **p["finding"]})
        elif t == "finding.adjudicated":
            adjudications.append({k: p.get(k) for k in
                                  ("finding_id", "verdict", "reason",
                                   "verdict_by")})
        elif t == "mission.finished":
            fin.update({k: p.get(k) for k in ("summary", "reason",
                                              "satisfied", "plateau_reason")})

    timeline.sort(key=lambda e: (e["step"], e["task"], e["seq"]))
    if step is not None:
        timeline = [e for e in timeline if e["step"] == step]
    if task is not None:
        timeline = [e for e in timeline if e["task"] == task]
        failures = [f for f in failures if f.get("task") == task]
        clues = [c for c in clues if c.get("task") == task]
    return {"plan": plan, "timeline": timeline, "failures": failures,
            "clues": clues, "findings": findings,
            "adjudications": adjudications, "finish": fin}


# ─────────────────────────── 文本渲染（确定性） ───────────────────────────

def render_text(tr: dict, root=None) -> str:
    lines: list[str] = []
    w = lines.append
    plan = tr["plan"]
    w(f"# secpt trace: {Path(root).name if root else ''}".rstrip())
    w("")
    w("== 计划 ==")
    w("")
    m = plan["mission"] or {}
    w(f"objective: {m.get('objective', '?')}")
    w(f"targets: {', '.join(m.get('targets', []))}")
    for i, p in enumerate(plan["plans"], 1):
        if "n" in p:
            w(f"plan#{i}: 任务 {p.get('n')}，策略 {json.dumps(p.get('strategy'), ensure_ascii=False, sort_keys=True)}，理由 {p.get('rationale', '')}")
        else:
            w(f"plan#{i}: 修订 {len(p.get('ops', []))} 个操作，理由 {p.get('rationale', '')}")
    for tk in plan["tasks"]:
        w(f"  - {tk['id']} [{tk['type']}] {tk['goal']}"
          f"（tools: {', '.join(tk['allowed_tools']) or '无'}；"
          f"判据 {tk['success_criteria']}；deps: "
          f"{', '.join(tk['deps']) or '无'}）")
    w("")
    w("== 时间线 ==")
    w("")
    if tr["timeline"]:
        for e in tr["timeline"]:
            extra = {k: v for k, v in e.items()
                     if k not in ("step", "task", "seq", "type")}
            detail = " ".join(f"{k}={_fmt(v)}" for k, v in extra.items())
            w(f"[step {e['step']}] {e['task']} #{e['seq']} {e['type']}"
              + (f" {detail}" if detail else ""))
    else:
        w("（无匹配条目）")
    w("")
    w("== 失败与重试 ==")
    w("")
    if tr["failures"]:
        for f in tr["failures"]:
            kind = f.get("kind") or f.get("to") or f.get("status") or "?"
            reason = f.get("reason") or (f.get("failure") or {}).get("detail")
            cls = (f.get("failure") or {}).get("cls")
            suffix = f"（{cls}）" if cls else ""
            w(f"- [step {f.get('step')}] {f.get('task')} {kind}{suffix}"
              + (f"：{reason}" if reason else ""))
    else:
        w("- 无")
    fin = tr["finish"]
    if fin:
        w(f"终态：{fin.get('reason', '?')}"
          + (f"（{fin.get('plateau_reason')}）" if fin.get("plateau_reason") else ""))
    w("")
    w("== 线索与裁决 ==")
    w("")
    w("线索：")
    if tr["clues"]:
        for c in tr["clues"]:
            w(f"- [{c['kind']}] {c['key']} = {c['value']}（task {c['task']}）")
    else:
        w("- 无")
    w("发现：")
    adj = {a["finding_id"]: a for a in tr["adjudications"]}
    if tr["findings"]:
        for f in tr["findings"]:
            a = adj.get(f["finding_id"], {})
            w(f"- F-{f['finding_id']} [{f['severity']}] {f['claim']}"
              f" → {a.get('verdict', '未裁决')}"
              + (f"：{a.get('reason')}" if a.get("reason") else ""))
    else:
        w("- 无")
    return "\n".join(lines) + "\n"


def render_json(tr: dict) -> str:
    return json.dumps(tr, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)
