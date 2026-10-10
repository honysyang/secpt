#!/usr/bin/env python3
"""DAG vs 单链 A/B 对照汇总。

扫 var/jobs/ 下 job_id 以 ab-dag- / ab-single- 开头的目录，
读各 job 的 events.jsonl，输出统计行 + 按 mode 分组的平均值。

统计口径（与 A/B 判据一致）：
    拿到flag = events 里出现 finding 事件且 payload.kind == 'flag'
    总步数   = assistant 事件数
    墙钟     = 首末事件 ts 差（秒）
    findings = finding 事件数
    错误调用 = tool_result 事件里 payload.status != 'ok'
"""

from __future__ import annotations

import json
from pathlib import Path

VAR = Path("var/jobs")


def analyze(job_dir: Path) -> dict:
    events = [
        json.loads(line)
        for line in (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assistant = sum(1 for e in events if e["type"] == "assistant")
    findings = sum(1 for e in events if e["type"] == "finding")
    err_calls = sum(
        1 for e in events if e["type"] == "tool_result" and e["payload"].get("status") != "ok"
    )
    flag = any(e["type"] == "finding" and e["payload"].get("kind") == "flag" for e in events)
    ts = [e["ts"] for e in events]
    wall = (ts[-1] - ts[0]) if ts else 0.0
    return {
        "assistant": assistant,
        "findings": findings,
        "err_calls": err_calls,
        "flag": flag,
        "wall": wall,
    }


def main() -> int:
    rows = []  # (job_id, mode, stats)
    for d in sorted(VAR.iterdir()):
        if not d.is_dir():
            continue
        jid = d.name
        if jid.startswith("ab-dag-"):
            mode = "dag"
        elif jid.startswith("ab-single-"):
            mode = "single"
        else:
            continue
        ev = d / "events.jsonl"
        if not ev.exists():
            continue
        rows.append((jid, mode, analyze(d)))

    if not rows:
        print("（未找到 ab-dag-* / ab-single-* 目录）")
        return 1

    print(
        f"{'job_id':<14s} {'mode':<7s} {'flag':<4s} {'steps':>5s} "
        f"{'wall_s':>9s} {'findings':>8s} {'err_calls':>9s}"
    )
    for jid, mode, r in rows:
        print(
            f"{jid:<14s} {mode:<7s} {'yes' if r['flag'] else 'no':<4s} "
            f"{r['assistant']:>5d} {r['wall']:>9.1f} {r['findings']:>8d} "
            f"{r['err_calls']:>9d}"
        )

    print("")
    for mode in ("dag", "single"):
        sel = [r for jid, m, r in rows if m == mode]
        if not sel:
            print(f"{mode:<7s} 无样本")
            continue
        n_ok = sum(1 for r in sel if r["flag"])
        avg_steps = sum(r["assistant"] for r in sel) / len(sel)
        avg_wall = sum(r["wall"] for r in sel) / len(sel)
        avg_err = sum(r["err_calls"] for r in sel) / len(sel)
        print(
            f"{mode:<7s} 成功率 {n_ok}/{len(sel)}  平均步数 {avg_steps:.1f}  "
            f"平均墙钟 {avg_wall:.1f}s  平均错误调用 {avg_err:.1f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
