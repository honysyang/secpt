"""上下文组装：四段式 + 窗口裁剪（BUILD_SPEC §7.1；ARCHITECTURE §8 L3）。

结构（约束 A/D）：
- system = phase prompt（身份+不变量+输出 schema）+ 工具清单。只放 phase 级
  不变内容：没有时间戳、进度、随机数，prefix cache 不破（§7.1 硬约束 1）。
- user = 简报 → 策略 → skills（放 user；core 恒为空）→ 任务块 → 线索 →
  经验 → <untrusted>（仅 worker，任务块之后、尾部指令之前，开闭标签永不
  裁剪）→ 尾部指令。

确定性（约束 B）：线索按 seq 升序取末 60；skills 按 id；工具按 name；
所有 JSON sort_keys。不依赖 dict 插入序或文件系统序。

裁剪（约束 C）：用户段总上限 MAX_CTX_CHARS；超限按固定顺序
线索（旧→新）→ 经验 → 任务细节 裁剪；acceptance / success_criteria /
current_strategy / scope / <untrusted> 标签永不裁剪。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from pa.assets import AssetSnapshot, render_tool_manifest
from pa.errors import SecptError
from pa.models import Clue, MissionState

PHASES = ("planner", "core", "worker")

MAX_CTX_CHARS = 120_000
CLUE_WINDOW = 60                    # 线索 count 窗口（末 60 条）
CLUE_CHAR_BUDGET = 20_000           # 线索字符预算（双上限，陷阱 A3）
LESSON_CHAR_BUDGET = 8_000

UNTRUSTED_OPEN = "<untrusted>"
UNTRUSTED_CLOSE = "</untrusted>"


@dataclass
class _Section:
    """user 的一个段。trim 非空表示参与全局裁剪（顺序 = 约束 C）。"""
    name: str
    text: str
    trim: str = ""          # ""（不裁剪）| "head"（旧→新砍头）| "tail"


def assemble(phase: str, state: MissionState, payload: dict,
             assets: AssetSnapshot) -> tuple[str, str]:
    """返回 (system, user)。同一 phase + 同一资产快照下 system 逐字节稳定。"""
    secs = render_sections(phase, state, payload, assets)
    system = secs.pop("system")
    sections = [_Section(k, t, trim=_TRIM_ORDER.get(k, ""))
                for k, t in secs.items()]
    return system, _fit(sections, MAX_CTX_CHARS)


def section_sizes(phase: str, state: MissionState, payload: dict,
                  assets: AssetSnapshot) -> dict:
    """各段尺寸与占比（S13 context.assembled 用）：只记数字，不记正文——
    不记目标、不记凭据。"""
    secs = render_sections(phase, state, payload, assets)
    total = sum(len(t) for t in secs.values())
    return {"total_chars": total,
            "sections": {k: {"chars": len(t),
                             "pct": round(len(t) / total, 4) if total else 0.0}
                         for k, t in sorted(secs.items())}}


# 参与全局裁剪的段及其方式（约束 C：线索 → 经验 → 任务细节，其余不裁）
_TRIM_ORDER = {"clues": "head", "lessons": "head", "task": "tail"}


def render_sections(phase: str, state: MissionState, payload: dict,
                    assets: AssetSnapshot) -> dict[str, str]:
    """渲染各段文本（裁剪前），返回 {"system": ..., "brief": ..., ...}。

    供诊断与测试：尺寸问题的归因必须能落到具体段，只看总量会掩盖增长源。"""
    if phase not in PHASES:
        raise SecptError(f"未知 phase: {phase}")

    out = {
        "system": "\n\n".join([
            assets.prompt(phase),
            render_tool_manifest(list(assets.tools)),
        ]),
        "brief": _render_brief(state),
        "strategy": _render_strategy(state),
        "skills": "",
        "task": _render_task(phase, state, payload),
        "criteria": _render_criteria(phase, payload),
    }
    if phase != "core":                       # 约束 A：skills 放 user，core 为空
        out["skills"] = _render_skills(phase, assets, payload)
    clues = payload.get("clues")
    if clues is None:
        clues = state.clues if phase == "core" else []
    out["clues"] = _render_clues(clues)
    lessons = _render_lessons(payload.get("lessons") or [])
    if lessons:
        out["lessons"] = lessons
    if phase == "worker":                     # 约束 D：仅 worker，任务块之后
        out["untrusted"] = _render_untrusted(payload)
    out["tail"] = _render_tail(phase)
    return out


# ── 渲染 ──────────────────────────────────────────────────────

def _render_brief(state: MissionState) -> str:
    spec = state.spec
    acc = "\n".join(f"- [{a.id}] {a.item}（证据要求: {a.evidence}）"
                    for a in spec.acceptance)
    tgts = "\n".join(f"- {t}" for t in spec.targets)
    scope = "\n".join(f"- {s}" for s in spec.allowed_scope) or "（空）"
    return ("# 任务简报\n"
            f"mission: {state.mission_id}\n"
            f"objective: {spec.objective}\n\n"
            f"## 目标清单\n{tgts}\n\n"
            f"## 验收标准（不裁剪）\n{acc}\n\n"
            f"## 范围（不裁剪）\n{scope}")


def _render_strategy(state: MissionState) -> str:
    return ("# 当前策略（不裁剪）\n```json\n"
            + json.dumps(state.current_strategy, ensure_ascii=False,
                         sort_keys=True, indent=1)
            + "\n```")


def _render_skills(phase: str, assets: AssetSnapshot, payload: dict) -> str:
    """分派定死（裁定）：core → 空（调用方保证不进来）；planner → 目录
    （id — trigger — 一句摘要），正文不给；worker → 按 task_type 匹配的
    全文，按 id 排序。"""
    if phase == "planner":
        picked = sorted(assets.skills, key=lambda s: s.id)
        if not picked:
            return ""
        lines = ["# 技能目录（按 id 排序；正文在执行阶段注入）"]
        for s in picked:
            lines.append(f"- {s.id} — {s.trigger} — {s.summary}")
        return "\n".join(lines)
    task_type = str(payload.get("type") or "")
    picked = assets.skills_for(task_type)
    if not picked:
        return ""
    blocks = ["# 技能（按 id 排序）"]
    for s in picked:
        blocks.append(f"## {s.id}\n{s.body}")
    return "\n\n".join(blocks)


def _render_task(phase: str, state: MissionState, payload: dict) -> str:
    if phase == "planner":
        return "# 任务\n制定首个计划。参考目标清单与范围。"
    if phase == "core":
        return (f"# 任务\n第 {payload.get('step', state.step)} 步决策："
                f"依据简报、策略与线索输出一个 Decision。")
    lines = [f"# 任务\n goal: {payload.get('goal', '')}",
             f"task_type: {payload.get('type', '')}",
             f"abort_criteria: {json.dumps(payload.get('abort_criteria', []), ensure_ascii=False)}",
             f"scope_slice: {json.dumps(payload.get('scope_slice', {}), ensure_ascii=False, sort_keys=True)}",
             f"allowed_tools: {json.dumps(payload.get('allowed_tools', []), ensure_ascii=False)}",
             f"failed_attempts: {payload.get('failed_attempts', 0)}"]
    return "\n".join(lines)


def _render_criteria(phase: str, payload: dict) -> str:
    sc = payload.get("success_criteria")
    if sc is None and phase == "worker":
        sc = []
    if not sc:
        return "# 成功判据（不裁剪）\n（本 phase 无）"
    items = "\n".join(f"- {json.dumps(c, ensure_ascii=False, sort_keys=True)}"
                      for c in sc)
    return f"# 成功判据（不裁剪）\n{items}"


def _clue_line(seq: int, c: Clue) -> str:
    val = json.dumps(c.value, ensure_ascii=False, sort_keys=True, default=str)
    return f"- [{seq}] {c.kind} {c.key}={val}"


def _render_clues(clues) -> str:
    """双上限（陷阱 A3）：按 seq 升序取末 CLUE_WINDOW 条；再按字符预算
    从旧到新砍到 CLUE_CHAR_BUDGET 以内。"""
    ordered = list(clues)
    if ordered and not isinstance(ordered[0], Clue):
        ordered = [Clue.model_validate(c) for c in ordered]
    window = ordered[-CLUE_WINDOW:]
    header = "# 线索（旧→新，可裁剪）"
    if not window:
        return header
    lines = [_clue_line(len(ordered) - len(window) + i + 1, c)
             for i, c in enumerate(window)]
    out, size = [header], len(header) + 1
    kept = []
    for ln in lines:
        if size + len(ln) + 1 > CLUE_CHAR_BUDGET:
            break
        kept.append(ln)
        size += len(ln) + 1
    return "\n".join(out + kept)


def _render_lessons(lessons: list) -> str:
    lines = ["# 经验（旧→新，可裁剪）"]
    for l in lessons:
        lines.append(f"- {json.dumps(l, ensure_ascii=False, sort_keys=True, default=str)}")
    return "\n".join(lines)


def _render_untrusted(payload: dict) -> str:
    items = payload.get("untrusted_data") or []
    body = "\n".join(f"[{i.get('source', '?')}] {i.get('data', '')}"
                     for i in items) or "（暂无工具产出）"
    return f"{UNTRUSTED_OPEN}\n{body}\n{UNTRUSTED_CLOSE}"


def _render_tail(phase: str) -> str:
    if phase == "worker":
        hint = "输出 call_tool 或 finish 的 Decision JSON，只输出 JSON。"
    elif phase == "planner":
        hint = "输出 PlanDraft JSON，只输出 JSON。"
    else:
        hint = "输出一个 Decision JSON，只输出 JSON。"
    return f"# 尾部指令\n{hint}"


# ── 全局裁剪（约束 C：线索 → 经验 → 任务细节）─────────────────

def _fit(sections: list[_Section], budget: int) -> str:
    def total() -> int:
        return sum(len(s.text) for s in sections) + 2 * (len(sections) - 1)

    for target in ("clues", "lessons", "task"):
        if total() <= budget:
            break
        for s in sections:
            if s.name != target or not s.trim:
                continue
            over = total() - budget
            if s.trim == "head":                 # 旧→新：从头部砍整行
                lines = s.text.split("\n")
                head, body = lines[0], lines[1:]
                while body and over > 0:
                    over -= len(body.pop(0)) + 1
                s.text = "\n".join([head] + body)
            else:                                # 任务细节：从尾部砍
                s.text = s.text[:-over] if over < len(s.text) else s.text
    return "\n\n".join(s.text for s in sections)
