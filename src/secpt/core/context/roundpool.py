"""S3 会话轮转 Session Roll Over。

⚠️ 本模块（rollover）是 Engine 里唯一允许"重建 messages"的地方；
   除此之外 Engine 内部任何位置都不许重建（红线：messages 整条 append）。

触发（任一满足）：
    steps_in_round >= round_max_steps | wall_in_round >= round_max_wall_s
    | est_tokens >= 0.55 * window | 子目标完成（subtask_done，默认 False）

动作：
    1) 用六段摘要模板生成 handoff
    2) messages = [system(objective+profile+scope+prior),
                   {role:user, content:"【上一回合交接】\n"+handoff}]
    3) round += 1，累计步数不重置（总预算看累计）
    4) 落 rounds/NNN.messages.json（由 hooks.on_rollover 完成）
"""

from __future__ import annotations

from secpt.core.context.estimator import est_messages_tokens

COMPACT_THRESHOLD = 0.55


def should_rollover(
    *,
    steps_in_round: int,
    wall_in_round: float,
    est_tokens: int,
    window: int,
    round_max_steps: int = 60,
    round_max_wall_s: int = 1500,
    subtask_done: bool = False,
) -> tuple[bool, str]:
    """判断是否轮转，返回 (是否, 原因)。"""
    if subtask_done:
        return True, "subtask_done"
    if round_max_steps > 0 and steps_in_round >= round_max_steps:
        return True, "max_steps"
    if round_max_wall_s > 0 and wall_in_round >= round_max_wall_s:
        return True, "max_wall"
    # 窗口触发的轮转是最后手段（重建链丢 prefix cache 代价高）：
    # 阈值贴近满窗（0.95），与 compaction 阈值（0.72）解耦——
    # 此前共用 0.55/0.72 导致压缩来不及救场时轮转风暴（实测 100 步 12 次轮转）
    if window > 0 and est_tokens >= 0.95 * window:
        return True, "window"
    return False, ""


def build_new_chain(system_msg: dict, handoff: str) -> list[dict]:
    """重建新链：system（原样保留）+ 上一回合交接（user）。

    注意：system 必须原样保留（含 objective+profile+scope+prior），
    handoff 走 user 角色（DeepSeek 对中途 system 消息处理不稳定）。
    """
    return [
        {"role": "system", "content": str(system_msg.get("content") or "")},
        {"role": "user", "content": f"【上一回合交接】\n{handoff}"},
    ]
