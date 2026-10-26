"""secpt log：可读的 mission 执行日志（叙事渲染，只读）。

回答四个问题：智能体想了什么（决策的 expect/rationale，随
action.started 落账）、做了什么（工具与参数）、执行得怎么样
（exit/证据输出摘要）、为什么收口（判定/教练建议/验收达成）。

硬规矩同 trace（S13）：只读 events.jsonl，不产生事件、不调写接口；
确定性——不渲染时间戳，同一账本渲染两次逐字节相等。
"""
from __future__ import annotations

import json
from pathlib import Path

from pa.core.evidence_kinds import acceptance_hit
from pa.store.view import StoreView

_OUT_LIMIT = 160          # 证据输出摘要的单字段截断长度


def render_log(root) -> str:
    root = Path(root)
    view = StoreView(root)
    events = list(view.events())
    state = view.restore()

    attempts: list[dict] = []            # 按派发顺序
    open_by_task: dict[str, dict] = {}   # 每任务至多一个在跑尝试（v0 同步）
    step = 1
    plans: list[dict] = []
    advisories: list[dict] = []
    spent_tokens = 0
    for ev in events:
        t, p = ev.type, ev.payload
        if t == "budget.charged":
            step += 1
            spent_tokens += int(p.get("tokens") or 0)
        elif t == "plan.created":
            plans.append(p)
        elif t == "mentor.advisory":
            advisories.append(p)
        elif t == "task.dispatched":
            a = {"task_id": p["task_id"], "attempt": p.get("attempt", 0),
                 "step": step, "actions": [], "status": None,
                 "failure": None}
            attempts.append(a)
            open_by_task[a["task_id"]] = a
        elif t in ("action.started", "action.blocked"):
            a = open_by_task.get(p.get("task_id"))
            entry = {"tool": p.get("tool"), "args": p.get("args") or {},
                     "fp": p.get("fp", ""), "expect": p.get("expect", ""),
                     "rationale": p.get("rationale", ""),
                     "exit": None, "evidence": None, "cached": False,
                     "blocked": p.get("reason") if t == "action.blocked"
                     else None}
            if a is None:                # 无派发记录的动作（理论外）照收
                a = {"task_id": p.get("task_id"), "attempt": 0,
                     "step": step, "actions": [], "status": None,
                     "failure": None}
                attempts.append(a)
            a["actions"].append(entry)
        elif t == "action.finished":
            a = open_by_task.get(p.get("task_id"))
            if a is None and p.get("task_id") is None and open_by_task:
                # 旧账本缓存回声不带 task_id：v0 同步执行，至多一个
                # 在跑尝试，归到它名下
                a = next(reversed(open_by_task.values()))
            entry = None
            if a is not None:
                for e in reversed(a["actions"]):     # 同 fp 配最后一笔
                    if e["fp"] == p.get("fp") and e["exit"] is None:
                        entry = e
                        break
            if entry is None:            # 缓存命中等无 started 的审计回声
                entry = {"tool": p.get("tool"), "args": {}, "fp": p.get("fp"),
                         "expect": "", "rationale": "", "exit": None,
                         "evidence": None, "cached": True, "blocked": None}
                if a is None:
                    a = {"task_id": p.get("task_id"), "attempt": 0,
                         "step": step, "actions": [], "status": None,
                         "failure": None}
                    attempts.append(a)
                a["actions"].append(entry)
            entry["exit"] = p.get("exit")
            entry["evidence"] = p.get("evidence")
            entry["cached"] = bool(p.get("cached"))
        elif t == "worker.returned":
            a = open_by_task.pop(p["task_id"], None)
            if a is not None:
                a["status"] = p.get("status")
                a["failure"] = p.get("failure")

    lines: list[str] = []
    w = lines.append
    mid = state.mission_id
    spec = state.spec
    w(f"# secpt 任务日志 {mid}")
    w("")
    w("## 任务")
    w("")
    w(spec.objective)
    if spec.intent:
        w(f"意图：{spec.intent}")
    if spec.dimensions:
        w(f"维度：{' / '.join(spec.dimensions)}")
    b = spec.budget
    w(f"目标：{'、'.join(spec.targets)} ｜ 范围：{'、'.join(spec.allowed_scope)}")
    w(f"预算：steps≤{b.steps} tokens≤{b.tokens} seconds≤{b.seconds}")
    w("")
    if plans:
        w("## 计划")
        w("")
        for i, p in enumerate(plans, 1):
            if len(plans) > 1:
                w(f"第 {i} 版计划：")
            if p.get("rationale"):
                w(f"理由：{p['rationale']}")
            st = p.get("strategy") or {}
            focus = st.get("focus")
            if focus:
                w(f"策略：{focus}")
        for tid in sorted(state.tasks):
            t = state.tasks[tid]
            cri = "、".join(c.evidence for c in t.success_criteria) or "无"
            w(f"- {tid} [{t.type}] {t.goal}（工具 {'、'.join(t.allowed_tools)}；"
              f"判据 {cri}）")
        w("")
    w("## 执行过程")
    w("")
    last_step = None
    for a in attempts:
        t = state.tasks.get(a["task_id"])
        title = f"{a['task_id']}"
        if t is not None:
            title += f" [{t.type}] {t.goal}"
        if a["attempt"]:
            title += f"（尝试 {a['attempt']}）"
        if a["step"] != last_step:
            w(f"### 第 {a['step']} 步")
            w("")
            last_step = a["step"]
        w(f"**{title}**")
        for e in a["actions"]:
            _render_action(w, view, e)
        status = a["status"] or "无结果"
        if a["failure"]:
            f = a["failure"]
            detail = f.get("detail") or f.get("cls") or ""
            w(f"- 判定：{status}（{f.get('cls')}：{detail}）")
        else:
            w(f"- 判定：{status}")
        adv = [x for x in advisories if x.get("target") == a["task_id"]]
        for x in adv:
            w(f"- 教练 {x.get('level')} {x.get('issue')}："
              f"本会话将 {x.get('would_do', '记录建议')}")
        w("")
    if advisories and not any(x.get("target") in state.tasks
                              for x in advisories):
        for x in advisories:
            w(f"- 教练 {x.get('level')} {x.get('issue')}（{x.get('target')}）")
        w("")
    if state.clues:
        w("## 线索")
        w("")
        for c in state.clues:
            src = f"（{c.source_task}）" if c.source_task else ""
            w(f"- [{c.kind}] {c.key} = {str(c.value)[:_OUT_LIMIT]}{src}")
        w("")
    w("## 收口")
    w("")
    reason = state.finish_reason or "未完成"
    pr = state.meta.get("plateau_reason")
    w(f"终止：{reason}" + (f"（{pr}）" if pr else ""))
    counts: dict[str, int] = {}
    for t in state.tasks.values():
        counts[t.state] = counts.get(t.state, 0) + 1
    w("- 任务：" + "、".join(f"{k} {v}" for k, v in sorted(counts.items())))
    sat = [a.id for a in spec.acceptance
           if any(acceptance_hit(c, a) for c in state.clues)]
    miss = [a.id for a in spec.acceptance if a.id not in sat]
    w(f"- 验收：{len(sat)}/{len(spec.acceptance)} 达成"
      + (f"（达成 {'、'.join(sat)}）" if sat else ""))
    if miss:
        w(f"- 未达成：{'、'.join(miss)}")
    n_steps = sum(1 for e in events if e.type == "budget.charged")
    w(f"- 结算：{n_steps} 步 · tokens {spent_tokens}")
    return "\n".join(lines) + "\n"


def _render_action(w, view: StoreView, e: dict) -> None:
    if e["cached"] and not e["args"]:
        args = "（同首次执行）"     # 缓存回声不重复 args（旧账本缺该字段）
    else:
        args = json.dumps(e["args"], ensure_ascii=False, sort_keys=True)
        if len(args) > _OUT_LIMIT:
            args = args[:_OUT_LIMIT] + "…"
    if e["blocked"]:
        w(f"- 决策：调用 {e['tool']}，参数 {args}")
        if e["expect"]:
            w(f"  预期：{e['expect']}")
        w(f"- 执行：被网关拦截（{e['blocked']}）——参数不含可达的范围内目标"
          if e["blocked"] == "missing_target" else
          f"- 执行：被网关拦截（{e['blocked']}）")
        return
    cached = "缓存命中" if e["cached"] else "实执行"
    if e["exit"] is None:
        w(f"- 决策：调用 {e['tool']}，参数 {args}（{cached}）")
    else:
        w(f"- 决策：调用 {e['tool']}，参数 {args}")
    if e["expect"]:
        w(f"  预期：{e['expect']}")
    if e["rationale"]:
        w(f"  理由：{e['rationale']}")
    if e["exit"] is not None:
        tail = f"，证据 {e['evidence']}" if e["evidence"] else ""
        w(f"- 执行：exit {e['exit']} · {cached}{tail}")
        snippet = _evidence_snippet(view, e["evidence"])
        if snippet:
            w(f"  输出：{snippet}")


def _evidence_snippet(view: StoreView, eid: str | None) -> str:
    if not eid:
        return ""
    try:
        content = view.evidence(eid)
    except Exception:
        return "（证据读取失败）"
    parts = []
    for key in ("stdout", "stderr"):
        s = str(content.get(key) or "").strip()
        if s:
            s = " ".join(s.split())          # 压成单行，可读性
            parts.append(f"{key}: {s[:_OUT_LIMIT]}"
                         + ("…" if len(s) > _OUT_LIMIT else ""))
    return " ｜ ".join(parts)
