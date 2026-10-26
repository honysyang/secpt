"""secpt CLI：run / resume / report / trace / stats / do / watch
（pyproject console_scripts 入口）。

退出码：0 completed（达成） / 2 budget（预算耗尽） / 3 plateau（平台期） /
4 halted（熔断：deadline|interrupted） / 1 error（异常；resume 不存在
的目录同样返回 1，不静默新建）。

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
from pa.report import log as log_mod
from pa.report import stats as stats_mod
from pa.report import trace as trace_mod
from pa.runtime import run
from pa.store.view import StoreView

_EXIT = {"completed": 0, "budget_steps": 2, "budget_tokens": 2,
         "plateau": 3, "deadline": 4, "interrupted": 4}   # 4 = halted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="secpt",
                                     description="无人值守安全测试智能体 v0")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="新目录引导 / 已有目录续跑")
    p_run.add_argument("dir")
    p_run.add_argument("--spec", help="mission JSON（新目录必填）")
    p_run.add_argument("--follow", action="store_true",
                       help="执行同时在当前终端跟随渲染事件流")
    p_resume = sub.add_parser("resume", help="恢复已存在目录并跑到终态")
    p_resume.add_argument("dir")
    p_report = sub.add_parser("report", help="渲染证据链报告 report.md")
    p_report.add_argument("dir")
    p_log = sub.add_parser("log", help="可读执行日志：决策/执行/结果/收口")
    p_log.add_argument("dir")
    p_trace = sub.add_parser("trace", help="只读还原 mission 执行轨迹（不产生事件）")
    p_trace.add_argument("dir")
    p_trace.add_argument("--step", type=int, help="只看某个超级步")
    p_trace.add_argument("--task", help="只看某个任务")
    p_trace.add_argument("--json", action="store_true", help="结构化输出")
    p_stats = sub.add_parser("stats", help="跨 mission 聚合统计与调参建议")
    p_stats.add_argument("dirs", nargs="+")
    p_stats.add_argument("--json", action="store_true", help="结构化输出")
    p_do = sub.add_parser("do", help="一句话任务 → 解析确认 → 引导并执行")
    p_do.add_argument("task", help="任务描述（如：扫描 10.0.0.5 的 80 端口）")
    p_do.add_argument("--dir", help="mission 目录（默认 missions/do-<hash>）")
    p_do.add_argument("--budget", help="预算文本（30分钟 / 2小时 / 1h），"
                                       "默认先任务文本后 15m")
    p_do.add_argument("--yes", action="store_true", help="跳过确认")
    p_do.add_argument("--dry-run", action="store_true",
                      help="只解析打印，不写目录不执行")
    p_do.add_argument("--follow", action="store_true",
                      help="执行同时在当前终端跟随渲染事件流")
    p_watch = sub.add_parser("watch", help="只读跟随已运行 mission 的事件流")
    p_watch.add_argument("dir")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "report":
            return _report(args.dir)
        if args.cmd == "log":
            print(log_mod.render_log(args.dir), end="")
            return 0
        if args.cmd == "trace":
            return _trace(args)
        if args.cmd == "stats":
            return _stats(args)
        if args.cmd == "do":
            return _do(args)
        if args.cmd == "watch":
            return _watch(args)
        return _run_mission(args)
    except SecptError as e:
        print(f"secpt: {e}", file=sys.stderr)
        return 1
    except Exception as e:                       # 1 异常：任何未预期错误
        print(f"secpt: 未预期错误 {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _do(args) -> int:
    """P1 解析（无目标 → SecptError → 退出 1，不建目录）→ P2 打印 →
    P3 确认（--yes 跳过，--dry-run 只打印）→ P4 写 mission.json 并 run()。"""
    from pa import nl

    llm = load_client()
    root = Path(args.dir) if args.dir else nl.default_dir(args.task)
    if (root / "events.jsonl").exists():
        # do 只引导新 mission：已有账本说明目录被占用，静默续跑会把
        # 旧任务重放一遍而用户以为在执行新任务——必须拒绝（且在 LLM
        # 调用之前，失败要快）
        raise SecptError(
            f"目录 {root} 已有事件账本：do 只引导新任务，"
            f"续跑请用 secpt resume，或换一个 --dir")
    spec, warnings = nl.build_spec(args.task, llm=llm,
                                   budget_text=args.budget)
    b = spec.budget
    print("─" * 56)
    print(f"任务：  {spec.objective}")
    if spec.intent:
        print(f"意图：  {spec.intent}")
    if spec.dimensions:
        print(f"维度：  {' / '.join(spec.dimensions[:5])}")
    print(f"目标：  {', '.join(spec.targets)}")
    print(f"范围：  {', '.join(spec.allowed_scope)}")
    for i, t in enumerate(spec.plan_sketch, 1):
        kind = f"[{t.kind}] " if t.kind else ""
        print(f"规划：  {i}. {kind}{t.goal}")
    print(f"预算：  steps≤{b.steps} tokens≤{b.tokens} "
          f"seconds≤{b.seconds} risk={b.risk}")
    print(f"环境：  {spec.env_class}")
    print(f"目录：  {root}（账本与日志位置）")
    for w in warnings:
        print(f"[!] {w}")
    print("─" * 56)
    if args.dry_run:
        print("dry-run：不写目录、不产生事件")
        return 0
    if not args.yes:
        try:
            ans = input("确认执行？[y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() not in ("y", "yes", "是"):
            print("已取消：未写目录、零事件")
            return 1
    if args.follow:
        return _run_follow(root, spec)
    state = run(root, spec, llm=llm)
    print(state.finish_summary)
    return _EXIT.get(state.finish_reason, 1)


def _watch(args) -> int:
    from pa.report import follow
    try:
        return follow.watch(args.dir)
    except KeyboardInterrupt:
        print("\nwatch 已退出（Ctrl-C），未改动任何文件", file=sys.stderr)
        return 130


def _run_mission(args) -> int:
    root = Path(args.dir)
    exists = (root / "events.jsonl").exists()
    if args.cmd == "resume":
        if not exists:
            # 退出 1，不新建（约束 G）
            raise SecptError(f"resume 的目录不存在或无事件账本: {root}")
        if getattr(args, "follow", False):
            return _run_follow(root, None)
        state = run(root, llm=load_client())     # run() 内部走 restore 路径
    else:
        spec = None
        if not exists:
            if not args.spec:
                raise SecptError("新目录引导需要 --spec mission.json")
            spec = MissionSpec.model_validate_json(
                Path(args.spec).read_text(encoding="utf-8"))
        if getattr(args, "follow", False):
            return _run_follow(root, spec)
        state = run(root, spec, llm=load_client())
    print(state.finish_summary)
    return _EXIT.get(state.finish_reason, 1)


def _run_follow(root: Path, spec) -> int:
    """run() 放守护线程，watch 在主线程渲染：Ctrl-C 只退 watch（130），
    守护线程里的 run 不中途写状态——但 Ctrl-C 后的行为与 run 单独被
    中断一致（interrupted 只在 run 主循环内处理）。"""
    import threading

    from pa.report import follow

    result: dict = {}
    t = threading.Thread(
        target=lambda: result.update(
            state=run(root, spec, llm=load_client())),
        daemon=True)
    t.start()
    try:
        code = follow.watch(root)
    except KeyboardInterrupt:
        print("\nCtrl-C：watch 退出（130）；run 在线程里，进程将结束",
              file=sys.stderr)
        return 130
    t.join(timeout=30)
    if "state" in result:
        print(result["state"].finish_summary)
    return code


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
