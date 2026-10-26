"""secpt status：mission 家族一览（只读，确定性排序）。

回答「我跑过什么、跑到哪了」：每个家族一行（runs 数、终态、步数、
tokens、任务分布、findings 裁决、墙钟），数据全部来自最新 run 的账本
（stats.collect 同口径）。这是命令行工作流的入口命令——先 status 看
全局，再 log/report 深入某一个。

设计取舍：聚合口径取「最新 run」而不是跨 run 合并——家族级合并是
report 的职责（save_report），status 要的是「最近一次跑到哪」的直觉。
"""

from __future__ import annotations

from pathlib import Path

from pa.errors import SecptError
from pa.infra.log import get_logger
from pa.report import stats as stats_mod

logger = get_logger("secpt.overview")


def latest_run(family: Path) -> Path | None:
    """家族 → 最新账本路径；从未跑过返回 None。

    统一规则：家族根裸账本视为 N=1，数字 runs/N 中最大者胜出——
    「最新一次尝试」是唯一真相（legacy 家族根 + runs/2 并存时，status/
    resume/log 都应指向 runs/2）。与 cli._run_path 共用此语义。"""
    family = Path(family)
    runs = family / "runs"
    candidates: dict[int, Path] = {}
    if runs.is_dir():
        for p in runs.iterdir():
            if p.name.isdigit() and (p / "events.jsonl").exists():
                candidates[int(p.name)] = p
    if (family / "events.jsonl").exists():
        candidates.setdefault(1, family)
    if not candidates:
        return None
    return candidates[max(candidates)]


def runs_count(family: Path) -> int:
    """已尝试的 run 数：数字 runs/N 目录 ∪ 根账本(N=1)。runs/N 目录存在
    但账本缺席（bootstrap 中断）也算一次尝试。"""
    family = Path(family)
    runs = family / "runs"
    nums = (
        {int(p.name) for p in runs.iterdir() if p.name.isdigit()}
        if runs.is_dir()
        else set()
    )
    if (family / "events.jsonl").exists():
        nums.add(1)
    return len(nums)


def _is_family(p: Path) -> bool:
    return p.is_dir() and (
        (p / "events.jsonl").exists() or (p / "runs").is_dir()
    )


def discover(roots) -> list[Path]:
    """根集合 → 家族列表。root 本身是家族就单列；是目录就扫一层子目录。
    去重（resolve 后路径）、按名排序——同一目录传两次不重复出行。"""
    fams: dict[str, Path] = {}
    for root in roots:
        root = Path(root)
        if _is_family(root):
            fams[str(root.resolve())] = root
            continue
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if _is_family(p):
                    fams[str(p.resolve())] = p
    return [fams[k] for k in sorted(fams, key=lambda k: fams[k].name)]


def collect(roots) -> list[dict]:
    """每个家族一行。账本损坏不炸整表：该行终态标「账本损坏」继续。"""
    rows: list[dict] = []
    for family in discover(roots):
        row: dict = {
            "name": family.name,
            "runs": runs_count(family),
            "terminal": "未跑",
            "steps": 0,
            "tokens": 0,
            "tasks": {},
            "findings": {},
            "wall_s": 0,
            "report": (family / "report.md").exists(),
        }
        latest = latest_run(family)
        if latest is None:
            rows.append(row)
            continue
        try:
            s = stats_mod.collect(latest)
        except (SecptError, Exception) as e:  # noqa: BLE001 —— 单行降级
            logger.warning("status 读取 %s 失败: %s", latest, e)
            row["terminal"] = "账本损坏"
            rows.append(row)
            continue
        row.update(
            terminal=str(s["terminal"]),
            steps=int(s["steps"]),
            tokens=int(s["tokens"]),
            tasks=dict(s["tasks"]),
            findings={
                "confirmed": int(s.get("findings_confirmed", 0)),
                "suspected": int(s.get("findings_suspected", 0)),
                "false_positive": int(s.get("findings_false_positive", 0)),
            },
            wall_s=int(s["wall_s"]),
        )
        rows.append(row)
    return rows


def render(rows: list[dict]) -> str:
    lines: list[str] = ["# secpt status", ""]
    if not rows:
        lines += ["（无 mission——用 secpt do <任务描述> 开始）", ""]
        return "\n".join(lines)
    head = (
        f"{'MISSION':<28} {'RUNS':>4} {'终态':<14} {'STEP':>4} {'TOKENS':>8} "
        f"{'任务(d/f/b)':>12} {'发现(c/s/f)':>12} {'墙钟':>6}"
    )
    lines += [head, "-" * len(head)]
    for r in rows:
        t = r["tasks"]
        dfb = f"{t.get('done', 0)}/{t.get('failed', 0)}/{t.get('blocked', 0)}"
        f = r["findings"]
        csf = (
            f"{f.get('confirmed', 0)}/{f.get('suspected', 0)}/"
            f"{f.get('false_positive', 0)}"
        )
        lines.append(
            f"{r['name']:<28} {r['runs']:>4} {r['terminal']:<14} {r['steps']:>4} "
            f"{r['tokens']:>8} {dfb:>12} {csf:>12} {r['wall_s']:>5}s"
        )
    lines.append("")
    lines.append("深入：secpt log <family> ｜ 流程重放：secpt log <family> --flow ｜ 报告：<family>/report.md")
    return "\n".join(lines) + "\n"
