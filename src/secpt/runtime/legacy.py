"""兼容入口：python -m secpt.runtime.legacy

行为与退役的 runtime/harness.py 完全一致（单 job、workspace=/tmp/harness），
用作 Engine 重构的回归对照。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from secpt.profiles import load_prior, render_system_extra
from secpt.runtime.engine import DEFAULT_BASH_WHITELIST, DEFAULT_FLAG_REGEX, Engine, RunConfig
from secpt.runtime.llm.client import call_llm


async def run(objective_file: str, attach_dir: str) -> int:
    workspace = Path("/tmp/harness")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY not set", file=sys.stderr)
        return 2

    # 读题面
    challenge_text = Path(objective_file).read_text(errors="replace")
    if attach_dir:
        ad = Path(attach_dir)
        if ad.is_dir():
            for f in sorted(ad.rglob("*")):
                if f.suffix in (".md", ".txt") and f.is_file():
                    challenge_text += f"\n\n--- {f} ---\n" + f.read_text(errors="replace")

    system_extra = render_system_extra(
        "ctf",
        objective=challenge_text,
        tools_doc=(
            f"- bash(command)  白名单命令：{', '.join(sorted(DEFAULT_BASH_WHITELIST))}，超时60s\n"
            "- read_file(path)  读文件，200KB上限\n"
            "- list_dir(path)  列目录，深度1\n"
            f"- write_file(path, content)  写文件到 {workspace}/ws/ 下"
            "（可写 python 脚本再 bash 执行）"
        ),
        prior=load_prior("ctf"),
    )

    cfg = RunConfig(
        job_id="legacy",
        objective=challenge_text,
        system_extra=system_extra,
        workspace=workspace,
        flag_regex=DEFAULT_FLAG_REGEX,
    )
    engine = Engine(cfg, llm=call_llm)
    result = await engine.run()
    return 0 if result.flag else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--objective-file", required=True)
    parser.add_argument("--attach-dir", default="")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.objective_file, args.attach_dir)))


if __name__ == "__main__":
    main()
