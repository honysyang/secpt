"""私钥护栏（keyguard）：阻止 PEM 私钥块进入 transcript / 上下文。

铁律：默认 profile 禁止加载私钥、禁止将私钥写入 transcript。工具输出
（bash cat 私钥、插件 dump 等）进入 messages 前经 redact_private_keys()，
命中块替换为占位符并计数，engine 侧据此写 audit(keyguard.redact)。
只拦「私钥材料」——证书/公钥/flag 不受影响。
"""

from __future__ import annotations

import re

# PEM 私钥块（含 RSA/EC/DSA/OPENSSH/ENCRYPTED/PGP 各变体），DOTALL 跨行匹配
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)


def detect_private_key(text: str) -> bool:
    """文本中是否含 PEM 私钥块。"""
    return _PEM_PRIVATE_KEY.search(text) is not None


def redact_private_keys(text: str) -> tuple[str, int]:
    """把 PEM 私钥块替换为占位符。返回（替换后文本, 命中块数）。"""

    def _sub(m: re.Match) -> str:
        return f"[REDACTED:private_key:{len(m.group(0))}B]"

    return _PEM_PRIVATE_KEY.subn(_sub, text)
