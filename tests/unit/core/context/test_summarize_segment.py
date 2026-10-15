"""E 修复：摘要全覆盖（分段摘要 + 合并），消除"删了没摘要到"。"""

from __future__ import annotations

import asyncio

from secpt.core.context.summarize import (
    LlmSummarizer,
    build_summary_prompt,
    segment_messages,
    transcript_text,
)


def _resp(content: str):
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _R:
        def __init__(self, c):
            self.choices = [type("c", (), {"message": _Msg(c)})()]

    return _R(content)


class _RecordingLLM:
    def __init__(self, fail_at: set[int] | None = None):
        self.prompts: list[str] = []
        self._fail_at = fail_at or set()

    async def call(self, messages, tools, model):
        idx = len(self.prompts)
        self.prompts.append(messages[0]["content"])
        if (idx + 1) in self._fail_at:
            raise RuntimeError(f"boom#{idx + 1}")
        return _resp("1. 任务目标：x\n6. 下一步候选：y")


def test_segment_messages_budget_and_coverage():
    msgs = [{"role": "user", "content": f"UNIQ{i}|" + "z" * 3000} for i in range(30)]
    segs = segment_messages(msgs, budget_chars=6000)
    assert len(segs) >= 3  # 每条渲染 ~500 字符 → 6000 预算约 11 条一段
    assert [m for seg in segs for m in seg] == msgs  # 不丢不重、保持顺序
    for seg in segs:
        assert len(transcript_text(seg)) <= 6000


def test_short_transcript_single_call_regression():
    """不超预算：单次调用（与旧行为一致，不多花 LLM 调用）。"""
    llm = _RecordingLLM()
    s = LlmSummarizer(llm)
    out = asyncio.run(s("目标", [{"role": "user", "content": "短对话"}]))
    assert out and len(llm.prompts) == 1
    assert llm.prompts[0] == build_summary_prompt(
        "目标", transcript_text([{"role": "user", "content": "短对话"}])
    )


def test_long_transcript_covers_all_segments():
    """超预算：分段摘要 + 合并；所有段标记都出现在某次 prompt 里（全覆盖）。"""
    llm = _RecordingLLM()
    s = LlmSummarizer(llm)
    msgs = [{"role": "user", "content": f"UNIQ{i}|" + "z" * 3000} for i in range(30)]
    out = asyncio.run(s("目标", msgs))
    seg_prompts = [p for p in llm.prompts if "只输出摘要正文，不要解释。" in p]
    merge_prompts = [p for p in llm.prompts if "分段摘要：" in p]
    assert len(seg_prompts) >= 3
    assert len(merge_prompts) >= 1
    joined = "\n".join(seg_prompts)
    for i in range(30):
        assert f"UNIQ{i}" in joined  # 不再只取头部一批
    assert out  # 返回合并后的摘要正文


def test_partial_segment_failure_degrades():
    """段级失败降级为占位并继续合并（部分摘要好过没有）。"""
    llm = _RecordingLLM(fail_at={2})
    s = LlmSummarizer(llm)
    msgs = [{"role": "user", "content": f"UNIQ{i}|" + "z" * 3000} for i in range(30)]
    out = asyncio.run(s("目标", msgs))
    assert out
    merge_prompt = next(p for p in llm.prompts if "分段摘要：" in p)
    assert "本段摘要失败" in merge_prompt


def test_all_segments_failed_raises():
    """全部段失败才抛（调用方的重试/失败兜底路径接手，不产出空摘要）。"""
    llm = _RecordingLLM(fail_at={1, 2, 3, 4, 5, 6, 7, 8, 9, 10})
    s = LlmSummarizer(llm)
    msgs = [{"role": "user", "content": f"UNIQ{i}|" + "z" * 3000} for i in range(30)]
    try:
        asyncio.run(s("目标", msgs))
        raise AssertionError("全部失败必须抛")
    except RuntimeError as e:
        assert "boom" in str(e)
