#!/usr/bin/env python3
"""生成 TaskSpec yaml（DAG vs 单链 A/B 实验用）。

用法:
    python scripts/make_spec.py <题面文件> <job_id> <entry1> <entry2> [...]

行为:
    读出题面全文作为 objective（块标量 |，不做转义成一行）；
    task_type=ctf；entries 取剩余参数。
    写入 cwd 下 spec.yaml，并打印前 20 行自查。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


class LiteralStr(str):
    """标记字符串以 YAML 块标量（|）输出，避免被转义成一行。"""


def _literal_representer(dumper: yaml.SafeDumper, data: LiteralStr):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.add_representer(LiteralStr, _literal_representer, Dumper=yaml.SafeDumper)


def main(argv: list[str]) -> int:
    # 选项（可组合，放在位置参数前）：
    #   --prior <name>      先验覆盖（knowledge/priors/<name>.md，如 saml）
    #   --artifact <path>   附件路径/目录（可多次），注入 objective 供 read_file 读
    #   --steps <n>         步数上限（0=不限，缺省用 profile）
    #   --wall <s>          墙钟上限秒（0=不限，缺省用 profile）
    prior = None
    artifacts: list[str] = []
    steps = wall = None
    pos: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--prior" and i + 1 < len(argv):
            prior = argv[i + 1]
            i += 2
        elif a == "--artifact" and i + 1 < len(argv):
            artifacts.append(argv[i + 1])
            i += 2
        elif a == "--steps" and i + 1 < len(argv):
            steps = int(argv[i + 1])
            i += 2
        elif a == "--wall" and i + 1 < len(argv):
            wall = int(argv[i + 1])
            i += 2
        else:
            pos.append(a)
            i += 1

    if len(pos) < 3:
        print(__doc__)
        return 2

    challenge_file, job_id = Path(pos[0]), pos[1]
    entries = pos[2:]

    if not challenge_file.is_file():
        print(f"Error: 题面文件不存在: {challenge_file}")
        return 2
    for a in artifacts:
        if not Path(a).exists():
            print(f"Error: 附件不存在: {a}")
            return 2

    objective = challenge_file.read_text(encoding="utf-8", errors="replace").strip()

    spec: dict = {
        "job_id": job_id,
        "task_type": "ctf",
        "objective": LiteralStr(objective),
        "entries": entries,
    }
    if prior:
        spec["prior"] = prior
    if artifacts:
        spec["artifacts"] = artifacts
    if steps is not None or wall is not None:
        spec["budget"] = {"steps": steps or 0, "wall_s": wall or 0}

    out = yaml.safe_dump(
        spec,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )

    Path("spec.yaml").write_text(out, encoding="utf-8")

    lines = out.splitlines()
    for line in lines[:20]:
        print(line)
    if len(lines) > 20:
        print(f"... (共 {len(lines)} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
