"""长期经验库（E3，WORKFLOW §4.4 L4 / §9.2-3 只读继承纪律）。

跨 mission 知识层 experiences.jsonl：mission 收口时由 LLM 提炼经验条目
追加入库；新 mission 启动（planner 引导）与每轮编排注入全库概览，worker
派发按任务类型精确匹配（applies_to ∩ task.type，空 = 通用条目）——不做
向量检索：条目是字面经验，任务类型是唯一检索维度。

三条纪律在此落地：

1. 只读继承（L249）：注入侧永远只读；写路径只有 distill 收口钩子一条，
   方向是「mission → 库」，注入侧（load / lessons_for）绝不产生写入；
2. 提炼幂等：条目带 origin（mission id），库内已有同 origin 条目即跳过——
   崩溃重入同一收口不会重复落条；entry 追加失败（盘满/权限）静默降级；
3. LLM 失败静默降级（§5.2 红线 3）：模型缺席（llm=None）/ phase 未注入 /
   输出不合法 → 无条目 + 告警，不阻塞 mission 收工——与 memory.reflect
   同款形态，所以「收口即经验」在 MockLLM 现有测试里自动走降级路径，
   旧行为不受影响。

条目 schema（数据驱动，与角色库 applies_to 维度对齐，L204「先做会返工」
的返工风险由 M1/M4 落地后消除）::

    {"applies_to": ["recon"],   # 适用任务类型（TaskDraft.type 精确匹配；
                                # 空或缺省 = 全类型通用）
     "lesson": str,             # 经验正文（LLM 提炼，≤400 字符）
     "origin": str,             # 产出 mission id（溯源 + 提炼幂等键）
     "inherited": true}         # 只读继承标（库内恒 true，L249 纪律）

存储位置：SECPT_EXPERIENCES 显式指定优先；缺省工作区级（mission 目录
父级 experiences.jsonl）——跨 mission 的层随工作区走，不落单个 mission
目录，也不进仓库根（陷阱 A5：运行数据进工作区）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, Field

from pa import envs
from pa.errors import ModelCallError, ModelParseError
from pa.infra.llm import complete_retry
from pa.infra.log import get_logger
from pa.models import MissionState

logger = get_logger("secpt.experience")

_SYSTEM = (
    "你是 secpt 的长期经验库提炼层（L4）。输入是一次 mission 的收口快照"
    "（目标 / 结束原因 / 任务分布 / findings 裁决 / 证据审计）。请提炼"
    "不超过 5 条可复用的经验，每条 400 字符以内：只写对同类任务真正有"
    "指导价值的法则（例如某类任务应先做什么、什么信号代表死路、什么"
    "工具参数组合有效、什么错误不要重犯）。不要复述目标与数字流水，"
    "不要编造输入里没有的事实。只输出 JSON："
    '{"lessons": [{"applies_to": ["类型"], "lesson": "..."}]}；'
    "applies_to 只能取输入任务分布里出现过的任务类型，通用经验留空数组。"
)


class DistillLesson(BaseModel):
    """单条经验（为什么用 schema 闸而非任意字段：与 reflect 同款，超限
    整体降级而不是静默截断——模型给的超长输出通常是失控征兆）。"""

    applies_to: list[str] = Field(default_factory=list, max_length=4)
    lesson: str = Field(min_length=1, max_length=400)


class DistillOut(BaseModel):
    lessons: list[DistillLesson] = Field(min_length=1, max_length=5)


def library_path(root: Path) -> Path:
    """经验库路径：SECPT_EXPERIENCES 显式指定优先；缺省 = 工作区级
    （mission 目录父级）。纯路径计算，无副作用、无写入。"""
    override = envs.experiences_path()
    return override if override is not None else root.parent / "experiences.jsonl"


def load(path: Path) -> list[dict]:
    """JSONL 容错加载：坏行 / 非 dict / 缺 lesson 正文逐行跳过 + 告警，
    好条目保持文件序（确定性：注入输出的排序只由追加序决定，不依赖
    文件系统序或 dict 序）。文件缺失按空库处理——首个 mission 的
    注入与提炼在空库上同样成立。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            logger.warning("经验库第 %d 行不是合法 JSON，跳过: %.40s…", i, line)
            continue
        if not isinstance(entry, dict) or not (
            isinstance(entry.get("lesson"), str) and entry["lesson"]
        ):
            logger.warning("经验库第 %d 行条目非法（缺 lesson 正文），跳过", i)
            continue
        out.append(entry)
    return out


def all_lessons(root: Path) -> list[dict]:
    """planner 引导 / orchestrator 每轮的注入视图：全库（使命级视角，
    此时还没有单一任务类型可锚定精确匹配）。"""
    return load(library_path(root))


def lessons_for(root: Path, task_types: Iterable[str]) -> list[dict]:
    """worker 注入视图：applies_to 与任务类型精确匹配；空 / 缺省
    applies_to = 通用条目恒注入。只读：本函数无任何写路径（L249）。"""
    types = set(task_types)
    kept: list[dict] = []
    for e in load(library_path(root)):
        at = e.get("applies_to")
        if not isinstance(at, list) or not at:  # 通用条目（含坏类型容错）
            kept.append(e)
        elif set(at) & types:
            kept.append(e)
    return kept


def distill(
    state: MissionState,
    root: Path,
    llm,
    *,
    finish_reason: str,
    audit_summary: dict | None = None,
) -> bool:
    """收口提炼钩子（在 exit_audit 之后、mission.finished 之前调用——
    顺序保证：断电重入时 mission 尚未 finished，_finish 会再走一遍，
    同 origin 幂等兜底不会重复落条）。

    成功追加条目返回 True；LLM 失败 / 幂等命中 / llm=None / 追加失败
    一律 False 静默降级——绝不阻塞收口路径。"""
    if llm is None:
        return False
    path = library_path(root)
    if any(e.get("origin") == state.mission_id for e in load(path)):
        logger.info("经验提炼幂等命中：mission %s 已入库，跳过", state.mission_id)
        return False
    try:
        raw = complete_retry(
            llm,
            retries=0,
            system=_SYSTEM,
            user=_distill_input(state, finish_reason, audit_summary),
            schema=DistillOut,
            phase="distill",
        )
        parsed = llm.parse(raw, DistillOut)
    except (ModelCallError, ModelParseError) as e:
        # §5.2 红线 3：经验是增强信息不是收口前提，失败记告警不阻塞
        logger.warning("经验提炼失败，静默降级: %s", type(e).__name__)
        return False
    from pa.adjudicate import finding_id

    done = sum(1 for t in state.tasks.values() if t.state == "done")
    confirmed = sum(
        1
        for f in state.findings
        if (state.adjudications.get(finding_id(f)) is not None)
        and state.adjudications[finding_id(f)].verdict == "confirmed"
    )
    entries = [
        {
            "applies_to": list(ls.applies_to),
            "lesson": ls.lesson,
            "origin": state.mission_id,
            "inherited": True,
            # 经验溯源：以后可按完成率/confirmed 数做检索过滤，而不是
            # 把每次失败/空跑的经验无差别注入新 mission。
            "finish_reason": finish_reason,
            "tasks_done": done,
            "clues": len(state.clues),
            "findings_confirmed": confirmed,
        }
        for ls in parsed.lessons
    ]
    return _append_entries(path, entries)


def _distill_input(
    state: MissionState, finish_reason: str, audit_summary: dict | None
) -> str:
    """进 LLM 的机械信号快照（user）：全部取自投影状态，确定性序列，
    只分类计数与裁决结论，不铺线索正文（会话无关细节不进库）。"""
    by_type_state: dict[tuple[str, str], int] = {}
    for t in state.tasks.values():
        k = (t.type, t.state)
        by_type_state[k] = by_type_state.get(k, 0) + 1
    dist = ", ".join(f"{ty}:{st} {n}" for (ty, st), n in sorted(by_type_state.items()))
    lines = [
        f"使命目标: {state.spec.objective}",
        f"结束原因: {finish_reason}",
    ]
    if dist:
        lines.append(f"任务分布: {dist}")
    from pa.adjudicate import finding_id
    from pa.core.evidence_kinds import acceptance_hit

    met = sum(
        1
        for a in state.spec.acceptance
        if any(acceptance_hit(c, a) for c in state.clues)
    )
    lines.append(f"验收: {met}/{len(state.spec.acceptance)} 项满足")
    if state.findings:
        fl: list[str] = []
        for f in state.findings[:10]:
            adj = state.adjudications.get(finding_id(f))
            v = adj.verdict if adj else "undecided"
            fl.append(f"{v} {f.claim[:80]}")
        lines.append("findings 裁决: " + "; ".join(fl))
    if audit_summary:
        # audit.audit_refs 的 broken 是 [{"finding":..., "refs":[...]}]；
        # 旧实现只数 str 导致永远 0，把断链信号喂成了“全绿”。
        broken = audit_summary.get("broken") or []
        n_bad = len(broken)
        lines.append(
            f"证据审计: 引用 {audit_summary.get('refs', 0)} 条，"
            f"可核验 {audit_summary.get('verified', 0)}，断链 {n_bad}"
        )
    return "\n".join(lines)


def _append_entries(path: Path, entries: list[dict]) -> bool:
    """追加写库（append-only JSONL）：失败 → False + 告警，静默降级。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except OSError as e:
        logger.warning("经验条目落库失败，静默降级: %s", e)
        return False
