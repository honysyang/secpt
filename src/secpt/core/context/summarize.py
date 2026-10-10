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

MAX_TRANSCRIPT_CHARS = 6000  # 送进摘要模型的对话截断上限（防 prompt 爆炸）


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


def transcript_text(messages: list[dict]) -> str:
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
    return "\n".join(lines)[:MAX_TRANSCRIPT_CHARS]


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
    """真实摘要器：调 deepseek-chat（便宜模型）生成六段摘要。"""

    def __init__(self, llm, model: str = "deepseek-chat", api_key: str | None = None) -> None:
        self.llm = llm
        self.model = model
        self.api_key = api_key

    async def __call__(self, objective: str, messages: list[dict]) -> str:
        prompt = build_summary_prompt(objective, transcript_text(messages))
        req = [{"role": "user", "content": prompt}]
        if hasattr(self.llm, "call"):
            resp = await self.llm.call(req, [], self.model)
        else:
            # 兼容 harness.py 函数签名 call_llm(messages, tools, model, api_key)
            resp = await self.llm(req, [], self.model, self.api_key or "")
        return (resp.choices[0].message.content or "").strip()


def get_summarizer(llm, *, model: str = "deepseek-chat", api_key: str | None = None):
    """按模式选择摘要器：SECPT_FAKE_LLM → stub，否则真实 LLM。"""
    if os.environ.get("SECPT_FAKE_LLM"):
        return StubSummarizer()
    return LlmSummarizer(llm, model=model, api_key=api_key)
