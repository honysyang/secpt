#!/usr/bin/env python3
"""transcript 回放：把 /tmp/harness/transcript.json 渲染成可读 markdown。

用法:
    python scripts/replay.py /tmp/harness/transcript.json
    python scripts/replay.py /tmp/harness/transcript.json --out replay.md
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

FLAG_RE = re.compile(r"[Ff][Ll][Aa][Gg]\{[^{}]{3,}\}")

THINK_MAX = 500
CALL_MAX = 300
RESULT_MAX = 800


def truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f" …(截断，共 {len(text)} 字)"


def main() -> None:
    parser = argparse.ArgumentParser(description="回放 harness transcript")
    parser.add_argument("transcript", help="transcript.json 路径")
    parser.add_argument("--out", "-o", help="输出 markdown 路径（默认 stdout）")
    args = parser.parse_args()

    p = Path(args.transcript)
    if not p.is_file():
        print(f"Error: 文件不存在 {p}", file=sys.stderr)
        sys.exit(1)

    messages = json.loads(p.read_text(encoding="utf-8"))
    lines: list[str] = []
    tool_counter: Counter[str] = Counter()
    nudge_count = 0
    flag_step = None
    step_no = 0

    for msg in messages:
        role = msg.get("role", "")

        if role == "system":
            # 区分原始 system prompt 和 nudge
            content = msg.get("content", "")
            if "你已经连续" in content or "没有获得新信息" in content:
                nudge_count += 1
                lines.append(f"> **[nudge #{nudge_count}]** {truncate(content, 200)}")
                lines.append("")
            else:
                lines.append("## system prompt")
                lines.append("")
                lines.append(truncate(content, 800))
                lines.append("")

        elif role == "assistant":
            step_no += 1
            lines.append(f"## step {step_no}")
            lines.append("")

            # 正文 / 推理
            content = msg.get("content", "") or ""
            reasoning = msg.get("reasoning_content", "") or ""
            think_parts = []
            if reasoning:
                think_parts.append(f"[推理] {reasoning}")
            if content:
                think_parts.append(content)
            think = "\n\n".join(think_parts)
            lines.append(f"**thought**: {truncate(think, THINK_MAX)}")
            lines.append("")

            # tool_calls
            tcs = msg.get("tool_calls", [])
            for tc in tcs:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                arguments = fn.get("arguments", "")
                tool_counter[name] += 1
                lines.append(f"**call**: `{name}`  args={truncate(arguments, CALL_MAX)}")
                lines.append("")

            # flag 检查
            if not flag_step:
                text = content + reasoning
                m = FLAG_RE.search(text)
                if m:
                    flag_step = step_no

        elif role == "tool":
            content = msg.get("content", "") or ""
            lines.append(f"**result**: {truncate(content, RESULT_MAX)}")
            lines.append("")

            # flag 也可能在工具输出里
            if not flag_step:
                m = FLAG_RE.search(content)
                if m:
                    flag_step = step_no

    # ---- 末尾汇总 ----
    lines.append("---")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- 总步数（assistant 消息数）: **{step_no}**")
    lines.append(f"- 消息总数: {len(messages)}")
    lines.append(f"- 卡死切换器触发: **{nudge_count}** 次")
    if flag_step:
        lines.append(f"- 命中 flag 的 step: **{flag_step}**")
    else:
        lines.append("- 命中 flag 的 step: 未命中")
    lines.append("")
    lines.append("### 工具调用分布")
    lines.append("")
    for name, cnt in tool_counter.most_common():
        lines.append(f"- `{name}`: {cnt} 次")
    lines.append("")

    out_text = "\n".join(lines)

    if args.out:
        Path(args.out).write_text(out_text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(out_text)


if __name__ == "__main__":
    main()
