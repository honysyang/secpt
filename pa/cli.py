"""secpt CLI：run / resume / report / trace / stats / do / watch / tui
（pyproject console_scripts 入口）。

退出码：0 completed（达成） / 2 budget（预算耗尽） / 3 plateau（平台期） /
4 halted（熔断：deadline|interrupted） / 1 error（异常；resume 不存在
的目录同样返回 1，不静默新建）。

LLM 客户端经 load_client() 装载：测试用 SECPT_LLM_MOCK 注入脚本，
真实运行配 SECPT_BASE_URL / SECPT_API_KEY / SECPT_MODEL。

家族目录与 runs/N：
- 同一任务文本 hash 到家族目录 missions/do-<hash8>（见 nl.default_dir）；
- do 在家族内开 runs/N（N 从 1 递增）子账本，N=1 兼容旧裸 hash 目录；
- 家族根 latest 是指向最新 runs/N 的相对符号链接，do/resume/report/
  watch/tui/log/trace/stats 一律解析它，用户不需要记 runs/N 路径。
"""

from __future__ import annotations

import argparse
import curses
import os
import sys
from pathlib import Path

from pa import envs
from pa.errors import SecptError
from pa.infra.llm import load_client
from pa.infra.log import configure
from pa.models import MissionSpec
from pa.report import render_report
from pa.report import log as log_mod
from pa.report import stats as stats_mod
from pa.report import trace as trace_mod
from pa.runtime import run
from pa.store.view import StoreView

_EXIT = {
    "completed": 0,
    "budget_steps": 2,
    "budget_tokens": 2,
    "plateau": 3,
    "deadline": 4,
    "interrupted": 4,
    "failed": 1,
}  # 4 = halted；failed = 引导/规划失败（未执行）


def _run_path(family: Path) -> Path:
    """把家族目录解析成实际账本 runs/N 路径（只读/续跑用）。

    统一规则（overview.latest_run）：数字 runs/N 最大者胜出，家族根裸
    账本视为 N=1——「最新一次尝试」是唯一真相。从未跑过返回 runs/1
    （调用方负责引导或报错）。
    """
    family = Path(family)
    from pa.report.overview import latest_run

    latest = latest_run(family)
    if latest is not None:
        return latest
    return family / "runs" / "1"


def _next_run_path(family: Path) -> Path:
    """do 专用：永远分配一个新的 runs/N，绝不复用已有账本。

    旧实现只要家族根有裸 hash 账本就固定选 runs/2——第二次 do 会 resume
    上一次规划失败的半成品账本，再用旧 spec 直接 plateau（真是 run
    do-4a1e630e/runs/2）。这里按已存在的数字目录 +1；家族根旧账本视为
    N=1，所以下一个至少是 2。
    """
    family = Path(family)
    runs = family / "runs"
    nums: list[int] = []
    if runs.is_dir():
        nums = [int(p.name) for p in runs.iterdir() if p.name.isdigit()]
    next_n = max(nums, default=0) + 1
    if (family / "events.jsonl").exists():
        next_n = max(next_n, 2)
    return runs / str(next_n)


def main(argv=None) -> int:
    # 日志只在 CLI 入口 configure：幂等，进程级一次，重复调用不叠加
    # handler；测试捕获告警用裸 propagation（不进这条路径）。
    configure(envs.log_level())
    parser = argparse.ArgumentParser(
        prog="secpt", description="无人值守安全测试智能体 v0"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="新目录引导 / 已有目录续跑")
    p_run.add_argument("dir")
    p_run.add_argument("--spec", help="mission JSON（新目录必填）")
    p_run.add_argument(
        "--follow", action="store_true", help="执行同时在当前终端跟随渲染事件流"
    )
    p_run.add_argument(
        "--flow",
        action="store_true",
        help="配合 --follow：渲染人类可读的流程叙事（默认是单行摘要）",
    )
    p_resume = sub.add_parser(
        "resume", help="恢复家族目录最近一次 run（latest）并跑到终态"
    )
    p_resume.add_argument("dir", help="家族目录（自动解析 latest → runs/N）")
    p_resume.add_argument(
        "--follow", action="store_true", help="恢复的同时跟随渲染事件流"
    )
    p_resume.add_argument(
        "--flow",
        action="store_true",
        help="配合 --follow：渲染人类可读的流程叙事",
    )
    p_report = sub.add_parser("report", help="渲染证据链报告 report.md")
    p_report.add_argument("dir")
    p_log = sub.add_parser("log", help="可读执行日志：决策/执行/结果/收口")
    p_log.add_argument("dir")
    p_log.add_argument(
        "--flow",
        action="store_true",
        help="改渲染流程叙事（do 的实时日志离线重放；默认是叙事日志）",
    )
    p_trace = sub.add_parser("trace", help="只读还原 mission 执行轨迹（不产生事件）")
    p_trace.add_argument("dir")
    p_trace.add_argument("--step", type=int, help="只看某个超级步")
    p_trace.add_argument("--task", help="只看某个任务")
    p_trace.add_argument("--json", action="store_true", help="结构化输出")
    p_stats = sub.add_parser("stats", help="跨 mission 聚合统计与调参建议")
    p_stats.add_argument("dirs", nargs="+")
    p_stats.add_argument("--json", action="store_true", help="结构化输出")
    p_status = sub.add_parser(
        "status", help="mission 家族一览（默认扫描 missions/，全局入口）"
    )
    p_status.add_argument(
        "roots",
        nargs="*",
        help="扫描根（家族目录或包含家族的目录）；缺省 missions/",
    )
    p_do = sub.add_parser("do", help="一句话任务 → 解析确认 → 引导并执行")
    p_do.add_argument("task", help="任务描述（如：扫描 10.0.0.5 的 80 端口）")
    p_do.add_argument(
        "--attachment",
        action="append",
        default=[],
        metavar="PATH",
        help="附件（文本材料，可重复）：内容并入任务理解，如授权范围清单 urls.txt",
    )
    p_do.add_argument(
        "--dir", help="家族目录（默认 missions/do-<hash>；账本自动落 runs/N）"
    )
    p_do.add_argument(
        "--budget", help="预算文本（30分钟 / 2小时 / 1h），默认先任务文本后 15m"
    )
    p_do.add_argument("--yes", action="store_true", help="跳过确认")
    p_do.add_argument(
        "--dry-run", action="store_true", help="只解析打印，不写目录不执行"
    )
    p_do.add_argument(
        "--follow",
        action="store_true",
        help="兼容参数：do 默认已实时渲染流程日志，无需此参数",
    )
    _do_view = p_do.add_mutually_exclusive_group()
    _do_view.add_argument(
        "--raw",
        action="store_true",
        help="打印每个事件的原始全量 payload（旧详情流；排查字段时用）",
    )
    _do_view.add_argument(
        "--quiet",
        action="store_true",
        help="不实时渲染事件流，只打印收口摘要（旧静默行为）",
    )
    p_watch = sub.add_parser("watch", help="只读跟随已运行 mission 的事件流")
    p_watch.add_argument("dir")
    p_watch.add_argument(
        "--flow",
        action="store_true",
        help="渲染人类可读流程叙事（默认单行摘要；支持家族目录/latest）",
    )
    p_tui = sub.add_parser(
        "tui",
        help="交互式事件面板：列表 + 选中事件全字段/裁决原因/证据全文（只读）",
    )
    p_tui.add_argument("dir")
    args = parser.parse_args(argv)

    def _resolve(path_str: str) -> str:
        # report/log/watch/tui/trace/stats 都接受家族目录：解析 latest
        # 符号链接到实际账本 runs/N；没有 runs/ 的旧裸 hash 目录原样返回。
        # 家族根没有 latest（家族还没有任何账本）时 _run_path 返回 runs/1，
        # 调用方按「不存在」处理。
        p = Path(path_str)
        latest = p / "latest"
        if latest.is_symlink():
            return str((p / latest.readlink()).resolve())
        return str(_run_path(p))

    try:
        if args.cmd == "report":
            return _report(_resolve(args.dir))
        if args.cmd == "log":
            root = _resolve(args.dir)
            if getattr(args, "flow", False):
                from pa.report.flow import Flow
                from pa.store.view import StoreView

                view = StoreView(root)
                print(Flow(root).render_all(view.events()), end="")
                return 0
            print(log_mod.render_log(root), end="")
            return 0
        if args.cmd == "trace":
            args.dir = _resolve(args.dir)
            return _trace(args)
        if args.cmd == "stats":
            args.dirs = [_resolve(d) for d in args.dirs]
            return _stats(args)
        if args.cmd == "status":
            return _status(args)
        if args.cmd == "do":
            return _do(args)
        if args.cmd == "watch":
            return _watch(args, _resolve(args.dir))
        if args.cmd == "tui":
            return _tui(args, _resolve(args.dir))
        return _run_mission(args)
    except SecptError as e:
        print(f"secpt: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # 1 异常：任何未预期错误
        print(f"secpt: 未预期错误 {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _do(args) -> int:
    """应用层薄壳：只做「接收 → 呈现 → 确认 → 转交运行」。

    P1 校验附件在调 LLM 之前（失败要快）→ P2 任务理解（pa.core.nl，
    智能体侧）→ P3 打印确认面板 → P4 确认后写 mission.json 并 run()。
    解析不在本层：cli 不猜目标、不落数值，MissionSpec 全由 nl 产出。"""
    from pa.core import nl

    llm = load_client()
    attachments = [Path(p) for p in args.attachment]
    for p in attachments:
        # 附件校验放最前：文件/目录都不存在不该消耗一次 LLM 调用才发现
        if not p.exists():
            raise SecptError(f"附件不存在: {p}")
    family = Path(args.dir) if args.dir else nl.default_dir(args.task)
    # do 永远新开 run；resume 才续跑 latest。不要把新解析的 spec 送进
    # 一个已有账本的 run()——run() 会走 resume 并静默忽略新 spec。
    root = _next_run_path(family)
    # 解析期一次 LLM 调用可能持续数十秒：立刻给出反馈避免「没反应」的
    # 观感（真跑 do-4a1e630e 教训）
    print("正在解析任务（一次 LLM 调用），稍候…")
    spec, warnings = nl.build_spec(
        args.task, llm=llm, budget_text=args.budget, attachments=attachments
    )
    file_info, file_warns = ([], [])
    if attachments:
        file_info, file_warns = nl.collect_files(attachments)
    b = spec.budget
    print("─" * 56)
    print(f"任务：  {spec.objective}")
    if attachments:
        print(f"附件：  {', '.join(str(a) for a in attachments)}")
        print(
            f"文件：  {len(file_info)} 个将复制进 workspace/_attachments/"
            "（引擎自动扫描：密钥/危险 sink/依赖 CVE）"
        )
    if spec.intent:
        print(f"意图：  {spec.intent}")
    if spec.dimensions:
        print(f"维度：  {' / '.join(spec.dimensions[:5])}")
    print(f"目标：  {', '.join(spec.targets)}")
    print(f"范围：  {', '.join(spec.allowed_scope)}")
    for i, t in enumerate(spec.plan_sketch, 1):
        kind = f"[{t.kind}] " if t.kind else ""
        print(f"规划：  {i}. {kind}{t.goal}")
    print(f"验收：  {len(spec.acceptance)} 项（收口合同——确认这些就是你想要的判据）")
    for a in spec.acceptance[:6]:
        pth = f" {a.path}" if getattr(a, "path", None) else ""
        print(f"  · {a.id} {a.evidence} @ {a.target}{pth} — {a.item}")
    print(
        f"预算：  steps≤{b.steps} tokens≤{b.tokens} seconds≤{b.seconds} risk={b.risk}"
    )
    print(f"环境：  {spec.env_class}")
    print(f"目录：  {root}（账本与日志位置）")
    for w in warnings + file_warns:
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
    if args.quiet:
        # --quiet：回到旧的静默样式——管道/脚本场景只要一份收口摘要
        if file_info:
            refs, stage_warns = nl.stage_files(
                file_info, root / "workspace" / "_attachments"
            )
            spec.files = refs
            for w in stage_warns:
                print(f"[!] {w}")
        state = run(root, spec, llm=llm)
        print(state.finish_summary)
        _print_next_hints(family)
        return _EXIT.get(state.finish_reason, 1)
    # do 默认实时流程叙事（planner/orchestrator/worker 的决策链），
    # --raw 才回到逐事件全 payload。先更新 latest 到本次 root：家族根有
    # 账本（旧裸 hash）→ latest 指向自身；否则指向 runs/N。
    _latest_link_to(family, root)
    if file_info:
        # 确认后暂存：复制进本 run 的 workspace/_attachments/，
        # manifest 随 mission.json 落账（resume/重放可见）
        refs, stage_warns = nl.stage_files(
            file_info, root / "workspace" / "_attachments"
        )
        spec.files = refs
        for w in stage_warns:
            print(f"[!] {w}")
    if args.raw:
        code = _run_follow(root, spec, verbose=True)
    else:
        code = _run_follow(root, spec, flow=True)
    _print_next_hints(family)
    return code


def _print_next_hints(family: Path) -> None:
    """do 收尾的下一步指引：日志/续跑（报告路径由流程收口块给出）。"""
    print("─" * 56)
    print(f"日志：  secpt log {family}")
    print(f"续跑：  secpt resume {family}")
    print("总览：  secpt status missions")


def _latest_link_to(family: Path, root: Path) -> Path:
    """原子更新家族根 latest 符号链接，直接指向本次 root（幂等）。

    - root == family（resume 旧裸 hash 账本）：latest 指向 "."；
    - root == family/runs/N：latest 指向 "runs/N"。
    tmp + os.replace + 目录 fsync，防掉电丢名。
    """
    family = Path(family)
    family.mkdir(parents=True, exist_ok=True)
    target = Path(".") if Path(root) == family else Path("runs") / Path(root).name
    link = family / "latest"
    tmp = family / f".latest.{os.getpid()}.tmp"
    tmp.symlink_to(target)
    os.replace(tmp, link)
    dfd = os.open(family, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _watch(args, root) -> int:
    from pa.report import follow

    try:
        return follow.watch(root, flow=getattr(args, "flow", False))
    except KeyboardInterrupt:
        print("\nwatch 已退出（Ctrl-C），未改动任何文件", file=sys.stderr)
        return 130


def _tui(args, root) -> int:
    from pa.report import tui

    try:
        return tui.watch(root)
    except curses.error as e:
        # 终端过窄/curses 初始化失败：明确报错而非堆栈，与 watch 同口径只读
        print(
            f"tui: 终端初始化失败（{e}）；可用 secpt watch {root} 回退",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\ntui 已退出（Ctrl-C），未改动任何文件", file=sys.stderr)
        return 130


def _status(args) -> int:
    """mission 家族一览：只读，不产生事件。"""
    from pa.report import overview

    roots = args.roots or ["missions"]
    print(overview.render(overview.collect(roots)), end="")
    return 0


def _run_mission(args) -> int:
    # run/resume 都接受家族目录：resume 续跑 latest，run 新目录引导/
    # 已有目录续跑走原语义（root 必须是账本路径）
    family = Path(args.dir)
    root = _run_path(family) if args.cmd == "resume" else family
    exists = (root / "events.jsonl").exists()
    if args.cmd == "resume":
        if not exists:
            # 退出 1，不新建（约束 G）
            raise SecptError(f"resume 的目录不存在或无事件账本: {family}")
        if getattr(args, "follow", False):
            return _run_follow(root, None, flow=getattr(args, "flow", False))
        state = run(root, llm=load_client())  # run() 内部走 restore 路径
    else:
        spec = None
        if not exists:
            if not args.spec:
                raise SecptError("新目录引导需要 --spec mission.json")
            spec = MissionSpec.model_validate_json(
                Path(args.spec).read_text(encoding="utf-8")
            )
        if getattr(args, "follow", False):
            return _run_follow(root, spec, flow=getattr(args, "flow", False))
        state = run(root, spec, llm=load_client())
    print(state.finish_summary)
    return _EXIT.get(state.finish_reason, 1)


def _run_follow(
    root: Path, spec, *, verbose: bool = False, flow: bool = False
) -> int:
    """run() 放守护线程，watch 在主线程渲染：Ctrl-C 只退 watch（130），
    守护线程里的 run 不中途写状态——但 Ctrl-C 后的行为与 run 单独被
    中断一致（interrupted 只在 run 主循环内处理）。

    flow=True：人类可读流程叙事（do 默认）；verbose=True：逐事件全
    payload（do --raw）；两者都为 False：单行摘要（run --follow）。
    """
    import threading

    from pa.report import follow

    result: dict = {}
    t = threading.Thread(
        target=lambda: result.update(state=run(root, spec, llm=load_client())),
        daemon=True,
    )
    t.start()
    try:
        code = follow.watch(root, verbose=verbose, flow=flow)
    except KeyboardInterrupt:
        print("\nCtrl-C：watch 退出（130）；run 在线程里，进程将结束", file=sys.stderr)
        return 130
    t.join(timeout=30)
    # 流程叙事已打印完整收口块（任务/验收/发现/成本/报告），不再重复
    # 一行 finish_summary；raw/单行模式保留原有收口行。
    if "state" in result and not flow:
        print(result["state"].finish_summary)
    return code


def _report(d: str) -> int:
    # 调用方已把 d 解析成实际账本 runs/N 路径；家族级聚合在 save_report
    # （run 收口时自动落盘），这里保留单账本渲染作为调试/补救入口。
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
