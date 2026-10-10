"""卡死切换器（StuckDetector）。

原样搬运自 runtime/harness.py，阈值改由调用方传入。
提醒消息以 user 角色注入（DeepSeek 对中途 system 消息处理不稳定）。
"""

from __future__ import annotations

import re

FLAG_RE = re.compile(r"[Ff][Ll][Aa][Gg]\{[^{}]{3,}\}")


def extract_keywords(text: str) -> set[str]:
    """从工具输出中提取关键词（文件路径、URL、HTTP 状态码、字段名）。"""
    kws: set[str] = set()
    # URL
    for m in re.finditer(r"https?://[^\s\"'<>]+", text):
        kws.add(m.group(0))
    # 文件路径
    for m in re.finditer(r"(?:/[\w.\-]+){2,}", text):
        kws.add(m.group(0))
    # HTTP 状态码
    for m in re.finditer(r"\b\d{3}\b", text):
        code = int(m.group(0))
        if 100 <= code < 600:
            kws.add(f"HTTP_{code}")
    # 字段名（JSON key 或 HTML 属性）
    for m in re.finditer(r'"([\w_]+)"\s*:', text):
        kws.add(f"field:{m.group(1)}")
    # flag 命中
    for m in FLAG_RE.finditer(text):
        kws.add(f"FLAG:{m.group(0)}")
    # 错误关键词
    for kw in ("timeout", "Connection refused", "401", "403", "404", "500",
               "Access Denied", "Permission denied", "not found"):
        if kw.lower() in text.lower():
            kws.add(f"err:{kw}")
    return kws


class StuckDetector:
    """卡死切换器：连续无新信息时注入提醒。"""

    def __init__(self, threshold: int = 6, increment: int = 2) -> None:
        self._seen: set[str] = set()
        self._no_progress = 0
        self._threshold = threshold
        self._increment = increment
        self.trigger_count = 0

    def update(self, tool_output: str) -> bool:
        """返回 True 表示有新信息，False 表示无进展。"""
        kws = extract_keywords(tool_output)
        new = kws - self._seen
        if new:
            self._seen.update(kws)
            self._no_progress = 0
            return True
        self._no_progress += 1
        return False

    def should_nudge(self) -> bool:
        return self._no_progress >= self._threshold

    def nudge(self) -> str:
        """触发一次提醒，阈值 +increment。"""
        self.trigger_count += 1
        self._threshold += self._increment
        self._no_progress = 0  # 重置计数，避免连续触发
        return (
            "你已经连续多步没有获得新信息了。换一层再试，不要在同一层继续换参数。\n"
            "可选的层：读源码 / 猜凭据 / 换身份或会话 / 换协议字段 / 换目标端点。\n"
            "先说清楚你现在在哪一层、为什么卡住，再决定换到哪一层。"
        )
