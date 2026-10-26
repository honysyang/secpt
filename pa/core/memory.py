"""轮末滚动简报（E2，WORKFLOW §4.3 简报 / §5.2 红线 / §8.3-2 冷区 digest）。

L3 = orchestrator 专用「轮末滚动 mission 记忆」：每轮末尾由 reflect 层
基于机械信号提炼一段进展印象（进展/卡点/下一步），落 memory.reflect
事件后投影到 state.brief（新覆盖旧 = 滚动）。三条红线在此落地：

1. 记忆不改写账本——本模块只追加事件、只投影派生字段；
2. 提炼幂等——idem key 取投影状态组合（step + clue/finding 计数），
   崩溃重入同一轮只写一次；跨轮偶合撞 key 漏一轮简报属良性（ rolling，
   下一轮就覆盖，不因此增加复合水位方案）；
3. LLM 失败静默降级——提炼异常不阻塞 mission 收工：无事件、记告警、
   state.brief 保持原值。MockLLM 未注入 "reflect" phase 的现有测试
   全部自动走本路径（回归零感知）。

冷区 digest（§8.3-2）：账本原文各轮独立永不丢；orchestrator 渲染层
（context._render_round_brief）超预算截断 + 指引按回放查 memory.reflect
事件。投影不做滚动压缩——账本 = 真相，视图 = 预算下的摘要。
"""

from __future__ import annotations

from pa.errors import ModelCallError, ModelParseError
from pa.infra.llm import complete_retry
from pa.infra.log import get_logger
from pa.models import MissionState, ReflectBrief
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

logger = get_logger("secpt.memory")

_SYSTEM = (
    "你是 secpt 的轮末反思层（memory）。输入是本轮结束时的机械信号"
    "（任务状态分布 / 异常任务 / 台账计数 / 上一轮简报）。请提炼一段"
    "不超过 800 字符的简报：本轮进展、当前卡点、下一轮建议（各一句话）。"
    '只依据输入信号，不要编造。只输出 JSON：{"summary": "..."}。'
)


def reflect_round(state: MissionState, writer: StoreWriter, llm) -> bool:
    """轮末反思（E2 第 7 步）：提炼简报并落账投影。

    成功写 memory.reflect 事件并投影 state.brief，返回 True；
    LLM 失败静默降级（无事件 + 告警），幂等命中返回 False。

    幂等 key 说明：结算前 state.step 未增（budget.charge 在 round 之后
    的 loop 里递增），所以 key 基 step 取的是本轮（重放确定）；后缀
    counts 是崩溃重入时的防线——同一轮重入时 clue/finding 不变才命中
    同 key。若跨轮 counts 与 step 恰好全等，漏一轮简报属良性。
    """
    idem = f"refl:{state.step}:{len(state.clues)}:{len(state.findings)}"
    try:
        raw = complete_retry(
            llm,
            retries=0,
            system=_SYSTEM,
            user=_snapshot(state),
            schema=ReflectBrief,
            phase="reflect",
        )
        parsed = llm.parse(raw, ReflectBrief)
        summary = str(parsed.summary)
    except (ModelCallError, ModelParseError) as e:
        # §5.2 红线 3：简报是增强信息不是决策依据——失败记告警不阻塞
        logger.warning("轮末简报提炼失败，静默降级: %s", type(e).__name__)
        return False
    ev = writer.append(
        "memory.reflect",
        "reflect",
        {"step": state.step, "summary": summary},
        idem,
    )
    if ev is None:  # 幂等命中（崩溃重入）：已写过，不重复投影
        return False
    apply_event(state, ev)
    return True


def _snapshot(state: MissionState) -> str:
    """机械信号摘要（进 LLM 的 user）：按状态归类的任务计数、异常任务
    单列（failed / blocked，带尝试次数与原因）、findings 台账计数与
    裁决分布、上一轮简报。全部取自投影状态，确定性序列。"""
    by_state: dict[str, int] = {}
    for t in state.tasks.values():
        by_state[t.state] = by_state.get(t.state, 0) + 1
    states = ", ".join(f"{k} {by_state[k]}" for k in sorted(by_state)) or "无任务"
    troubles: list[str] = []
    for t in state.tasks.values():
        if t.state == "failed":
            troubles.append(f"{t.id} failed(尝试{t.attempt})")
        elif t.state == "blocked":
            r = t.block_reason or ""
            troubles.append(f"{t.id} blocked（{r[:60]}）")
    verdicts: dict[str, int] = {}
    for adj in state.adjudications.values():
        verdicts[adj.verdict] = verdicts.get(adj.verdict, 0) + 1
    prev = state.brief.get("summary", "") if state.brief else ""
    lines = [
        f"上一轮简报: {prev if prev else '（无）'}",
        f"任务状态: {states}",
    ]
    if troubles:
        lines.append("异常任务: " + "; ".join(sorted(troubles)))
    lines.append(
        f"发现台账: 提案 {len(state.findings)} 条，裁决 "
        + (
            ", ".join(f"{k} {verdicts[k]}" for k in sorted(verdicts))
            if verdicts
            else "暂无"
        )
        + f"；线索 {len(state.clues)} 条"
    )
    return "\n".join(lines)
