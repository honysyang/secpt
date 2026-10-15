"""S3 六段式摘要：用于 Compaction 与 Rollover 的上下文重建。

强制六段：
    1. 任务目标（原样复述 objective）
    2. 已确认事实（每条附证据来源，如 file:line 或 step N）
    3. 已获取凭据
    4. 已排除的尝试（务必写清，防止模型重复踩坑）
    5. 当前进展到哪一步
    6. 下一步候选（2-3 条）

红线：摘要只用于重建上下文，不参与决策/评分/剪枝；不用摘要替代工具原始结果。

SECPT_FAKE_LLM=1 时用 StubSummarizer（保证事件里也含"已排除"段落文本，
且不依赖真实 API）。
"""

from __future__ import annotations

import os

MAX_TRANSCRIPT_CHARS = 6000  # 单次送进摘要模型的对话渲染上限（分段预算）
_MERGE_BATCH = 8  # 分段摘要合并的批大小（超出则多级 reduce）


def build_summary_prompt(objective: str, transcript: str) -> str:
    """构造六段式摘要 prompt。"""
    return (
        "把下面的渗透任务对话历史压缩成结构化摘要，必须包含以下六段：\n"
        "1. 任务目标（原样复述）\n"
        "2. 已确认事实（每条附证据来源，如 file:line 或 step N）\n"
        "3. 已获取凭据\n"
        "4. 已排除的尝试（务必写清，防止模型重复踩坑）\n"
        "5. 当前进展到哪一步\n"
        "6. 下一步候选（2-3 条）\n\n"
        f"任务目标原文：\n{objective}\n\n"
        "对话历史（按时间顺序，越靠后越新）：\n"
        f"{transcript}\n\n"
        "只输出摘要正文，不要解释。"
    )


_MERGE_PROMPT = (
    "把下面的分段摘要合并成一份结构化总摘要，必须保留全部事实/凭据/已排除的尝试，"
    "按六段式组织（1 任务目标 / 2 已确认事实 / 3 已获取凭据 / 4 已排除的尝试 / "
    "5 当前进展 / 6 下一步候选），不要新增原文没有的信息。\n\n"
    "任务目标原文：\n{objective}\n\n分段摘要：\n{parts}\n\n只输出合并后的摘要正文。"
)


def transcript_text(messages: list[dict], *, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """把 messages 渲染成纯文本供摘要（system 跳过，内容截断）。"""
    lines = []
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        if role == "system":
            continue
        content = str(m.get("content") or "")
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                lines.append(
                    f"[step {i}] tool_call: {fn.get('name')}({str(fn.get('arguments', ''))[:200]})"
                )
        elif role == "tool":
            lines.append(f"[step {i}] tool_result: {content[:300]}")
        elif content.strip():
            lines.append(f"[step {i}] {role}: {content[:500]}")
    return "\n".join(lines)[:max_chars]


def segment_messages(
    messages: list[dict], *, budget_chars: int = MAX_TRANSCRIPT_CHARS
) -> list[list[dict]]:
    """按渲染预算把被删消息切成若干段（每段渲染 ≤ budget，段数 ≥1）。

    E 修复：旧实现只把 transcript 头部若干字符送进摘要——单次删 200 组时
    后面的组"删了但没被摘要到"（静默信息丢失）。分段保证**全覆盖**：
    逐段摘要后合并（见 LlmSummarizer）。单条消息自身超预算时独占一段
    （渲染层已做逐条截断，实际不会超）。
    """
    if not messages:
        return [[]]
    segs: list[list[dict]] = []
    cur: list[dict] = []
    cur_len = 0
    for m in messages:
        piece_len = len(transcript_text([m], max_chars=10**9)) + 1
        if cur and cur_len + piece_len > budget_chars:
            segs.append(cur)
            cur, cur_len = [], 0
        cur.append(m)
        cur_len += piece_len
    if cur:
        segs.append(cur)
    return segs


class StubSummarizer:
    """fake 模式摘要器：返回六段模板文本，含"已排除的尝试"段。"""

    async def __call__(self, objective: str, messages: list[dict]) -> str:
        return (
            "1. 任务目标：" + (objective[:200] if objective else "（无）") + "\n"
            "2. 已确认事实：无（fake 模式，未调用真实 LLM）\n"
            "3. 已获取凭据：无\n"
            "4. 已排除的尝试：无（fake 模式）\n"
            "5. 当前进展：进行中\n"
            "6. 下一步候选：占位（fake 模式）"
        )


class LlmSummarizer:
    """真实摘要器：调便宜模型生成六段摘要。

    覆盖保证（E 修复）：被删消息渲染超预算时分段摘要（每段 ≤ 6000 字符渲染）
    再多级合并——不再只摘要头部一批。段级失败降级为占位文本继续合并；
    全部段失败才抛（由调用方的重试/失败兜底路径接手）。
    """

    def __init__(self, llm, model: str = "deepseek-chat", api_key: str | None = None) -> None:
        self.llm = llm
        self.model = model
        self.api_key = api_key

    async def _call(self, prompt: str) -> str:
        req = [{"role": "user", "content": prompt}]
        if hasattr(self.llm, "call"):
            resp = await self.llm.call(req, [], self.model)
        else:
            # 兼容 harness.py 函数签名 call_llm(messages, tools, model, api_key)
            resp = await self.llm(req, [], self.model, self.api_key or "")
        return (resp.choices[0].message.content or "").strip()

    async def __call__(self, objective: str, messages: list[dict]) -> str:
        segs = segment_messages(messages)
        if len(segs) <= 1:
            return await self._call(build_summary_prompt(objective, transcript_text(messages)))
        parts: list[str] = []
        failures = 0
        last_err: Exception | None = None
        for i, seg in enumerate(segs, 1):
            try:
                part = await self._call(build_summary_prompt(objective, transcript_text(seg)))
                parts.append(f"【第 {i}/{len(segs)} 段】\n{part}")
            except Exception as e:
                failures += 1
                last_err = e
                parts.append(f"【第 {i}/{len(segs)} 段】（本段摘要失败）")
        if failures == len(segs):
            raise last_err or RuntimeError("全部段摘要失败")
        while len(parts) > 1:
            parts = [
                await self._call(
                    _MERGE_PROMPT.format(objective=objective, parts="\n\n".join(batch))
                )
                for batch in (
                    parts[i : i + _MERGE_BATCH] for i in range(0, len(parts), _MERGE_BATCH)
                )
            ]
        return parts[0]


def get_summarizer(llm, *, model: str = "deepseek-chat", api_key: str | None = None):
    """按模式选择摘要器：SECPT_FAKE_LLM → stub，否则真实 LLM。"""
    if os.environ.get("SECPT_FAKE_LLM"):
        return StubSummarizer()
    return LlmSummarizer(llm, model=model, api_key=api_key)
