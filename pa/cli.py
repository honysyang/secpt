"""secpt CLI：run / resume / report（pyproject console_scripts 入口）。

退出码（S11 约束 G）：
  0 达成（completed） / 2 预算（budget_steps|budget_tokens） /
  3 平台期（plateau） / 4 熔断（deadline|interrupted） / 1 异常。
resume 一个不存在的目录返回 1——不许静默新建。

LLM 客户端经 load_client() 装载：测试用 SECPT_LLM_MOCK 注入脚本，
真实运行配 SECPT_BASE_URL / SECPT_API_KEY / SECPT_MODEL。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pa.errors import SecptError
from pa.infra.llm import load_client
from pa.models import MissionSpec
from pa.report import render_report
from pa.report import stats as stats_mod
from pa.report import trace as trace_mod
from pa.runtime import run
from pa.store.view import StoreView

_EXIT = {"completed": 0, "budget_steps": 2, "budget_tokens": 2,
         "plateau": 3, "deadline": 4, "interrupted": 4}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="secpt",
                                     description="无人值守安全测试智能体 v0")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="新目录引导 / 已有目录续跑")
    p_run.add_argument("dir")
    p_run.add_argument("--spec", help="mission JSON（新目录必填）")
    p_resume = sub.add_parser("resume", help="恢复已存在目录并跑到终态")
    p_resume.add_argument("dir")
    p_report = sub.add_parser("report", help="渲染证据链报告 report.md")
    p_report.add_argument("dir")
    p_trace = sub.add_parser("trace", help="只读还原 mission 执行轨迹（不产生事件）")
    p_trace.add_argument("dir")
    p_trace.add_argument("--step", type=int, help="只看某个超级步")
    p_trace.add_argument("--task", help="只看某个任务")
    p_trace.add_argument("--json", action="store_true", help="结构化输出")
    p_stats = sub.add_parser("stats", help="跨 mission 聚合统计与调参建议")
    p_stats.add_argument("dirs", nargs="+")
    p_stats.add_argument("--json", action="store_true", help="结构化输出")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "report":
            return _report(args.dir)
        if args.cmd == "trace":
            return _trace(args)
        if args.cmd == "stats":
            return _stats(args)
        return _run_mission(args)
    except SecptError as e:
        print(f"secpt: {e}", file=sys.stderr)
        return 1
    except Exception as e:                       # 1 异常：任何未预期错误
        print(f"secpt: 未预期错误 {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _run_mission(args) -> int:
    root = Path(args.dir)
    exists = (root / "events.jsonl").exists()
    if args.cmd == "resume":
        if not exists:
            # 退出 1，不新建（约束 G）
            raise SecptError(f"resume 的目录不存在或无事件账本: {root}")
        state = run(root, llm=load_client())     # run() 内部走 restore 路径
    else:
        spec = None
        if not exists:
            if not args.spec:
                raise SecptError("新目录引导需要 --spec mission.json")
            spec = MissionSpec.model_validate_json(
                Path(args.spec).read_text(encoding="utf-8"))
        state = run(root, spec, llm=load_client())
    print(state.finish_summary)
    return _EXIT.get(state.finish_reason, 1)


def _report(d: str) -> int:
    root = Path(d)
    view = StoreView(root)
    if not view.exists():
        raise SecptError(f"目录不存在或无事件账本: {root}")
    state = view.restore()
    text = render_report(state, view)
    out = root / "report.md"
    out.write_text(text, encoding="utf-8")
    print(f"报告已写入 {out}")
    return 0


def _trace(args) -> int:
    tr = trace_mod.build(args.dir, step=args.step, task=args.task)
    if args.json:
        print(trace_mod.render_json(tr), end="")
    else:
        print(trace_mod.render_text(tr, root=args.dir), end="")
    return 0


def _stats(args) -> int:
    rows = [stats_mod.collect(d) for d in args.dirs]
    if args.json:
        print(stats_mod.render_json(rows), end="")
    else:
        print(stats_mod.render(rows), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
