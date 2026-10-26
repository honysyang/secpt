"""动作指纹（BUILD_SPEC §6.5 公式 + S4 追加验收：剔除 VOLATILE_KEYS）。

§6.5 公式：sha256(f"{tool}|{json.dumps(args, sort_keys=True, ensure_ascii=False)}")[:32]。
追加验收：args 里的 timeout/retries/delay/jitter 只影响"怎么跑"，不影响
"做过没有"——剔除后再哈希，同一目标 timeout 60 与 120 得到同一指纹，
换端口/换目标则不同。指纹是动作去重与断点续做的键（§6.5 第 3 步）。
"""

from __future__ import annotations

import hashlib
import json

VOLATILE_KEYS = frozenset({"timeout", "retries", "delay", "jitter"})


def fingerprint(tool: str, args: dict) -> str:
    stable = {k: v for k, v in args.items() if k not in VOLATILE_KEYS}
    norm = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{tool}|{norm}".encode("utf-8")).hexdigest()[:32]
