"""S3 token 估算。

规则（不用 tiktoken，DeepSeek 分词器不公开，近似没意义）：
- 中文按 1 字 ≈ 0.7 token
- ASCII 按 1 token ≈ 4 字符（即 0.25 token/字）
- 保守起见：取 max(混合精确估计, 统一保守估计 len/2)，再乘 1.1 安全系数

窗口大小可通过环境变量 SECPT_FAKE_WINDOW 覆盖（验收用：假装窗口只有 8K，
强制触发压缩与轮转多次）。
"""

from __future__ import annotations

import os

SAFETY = 1.1
CJK_PER_CHAR = 0.7          # 中文 1 字 ≈ 0.7 token
ASCII_PER_CHAR = 0.25       # 1 token ≈ 4 字符
CONSERVATIVE_PER_CHAR = 0.5  # 统一保守估计：len/2
DEFAULT_WINDOW = 64_000     # 默认上下文窗口（token）


def _is_cjk(ch: str) -> bool:
    """粗略判断 CJK 字符（中日韩统一表意 + 全角标点）。"""
    return (
        "\u4e00" <= ch <= "\u9fff" or     # 中日韩统一表意文字
        "\u3000" <= ch <= "\u303f" or     # CJK 标点
        "\uff00" <= ch <= "\uffef" or     # 全角字符
        "\uac00" <= ch <= "\ud7af"        # 谚文
    )


def est_tokens(text: str) -> int:
    """估算一段文本的 token 数（保守，上浮）。"""
    if not text:
        return 0
    n = len(text)
    n_cjk = sum(1 for ch in text if _is_cjk(ch))
    n_ascii = n - n_cjk
    precise = n_cjk * CJK_PER_CHAR + n_ascii * ASCII_PER_CHAR
    conservative = n * CONSERVATIVE_PER_CHAR
    return max(1, int(max(precise, conservative) * SAFETY))


def est_message_tokens(msg: dict) -> int:
    """估算一条消息的 token 数（含 tool_calls 的 name+arguments）。"""
    parts = [str(msg.get("content") or "")]
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        parts.append(str(fn.get("name") or ""))
        parts.append(str(fn.get("arguments") or ""))
    return est_tokens("".join(parts))


def est_messages_tokens(messages: list[dict]) -> int:
    """估算整条链的 token 数。"""
    return sum(est_message_tokens(m) for m in messages)


def effective_window(default: int = DEFAULT_WINDOW) -> int:
    """读取上下文窗口；SECPT_FAKE_WINDOW 环境变量可覆盖（验收强制触发用）。"""
    v = os.environ.get("SECPT_FAKE_WINDOW")
    if v and v.strip().isdigit():
        return max(1, int(v.strip()))
    return default
