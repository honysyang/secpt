#!/usr/bin/env python3
"""构建前生成镜像知识种子：把历史 playbook/经验脱敏后放进 seed/。

红线：镜像会提交到平台——任何 token、flag、私钥、UUID 形态的敏感值都不能进镜像。
只保留"打法/技术"文本；命中敏感模式一律打码。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "var" / "knowledge"
DST = ROOT / "seed" / "knowledge"

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # 允许未闭合（digest 截断到一半的 flag 也必须打码）
    (re.compile(r"flag\{[^}\n]*\}?"), "flag{***}"),
    (re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.S), "[REDACTED KEY]"),
    (re.compile(r"(?i)(BENCHMARK_TOKEN|DEEPSEEK_API_KEY|API_KEY|SECRET)\s*[:=]\s*\S+"), r"\1=***"),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "***",
    ),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "sk-***"),
]


def scrub(text: str) -> str:
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    return text


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    pdir = SRC / "playbooks"
    n_pb = 0
    if pdir.is_dir():
        out_pb = DST / "playbooks"
        out_pb.mkdir(exist_ok=True)
        for f in sorted(pdir.glob("*.md")):
            (out_pb / f.name).write_text(scrub(f.read_text(encoding="utf-8")), encoding="utf-8")
            n_pb += 1
    n_exp = 0
    exp = SRC / "experiences.jsonl"
    if exp.is_file():
        rows: list[dict] = []
        for line in exp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            for k in ("title", "what_worked"):
                if isinstance(d.get(k), str):
                    d[k] = scrub(d[k])
            if isinstance(d.get("pitfalls"), list):
                d["pitfalls"] = [scrub(str(x)) for x in d["pitfalls"]]
            rows.append(d)
        (DST / "experiences.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )
        n_exp = len(rows)
    print(f"[seed] playbooks={n_pb} experiences={n_exp} -> {DST}")


if __name__ == "__main__":
    main()
