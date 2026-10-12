"""secpt solve：一键解题（易用性主入口）。

    secpt solve challenges/rois-saml-sso            # 挑战目录
    secpt solve challenges/rois-saml-sso/test.md    # 题面文件
    secpt solve <dir> --prior saml --wall 1800      # 覆盖项仍可干预
    secpt solve <dir> --no-judge --no-learn         # 关掉自动评判/学习

自动化（全部通用机制，可迁移）：
1. 入口抽取：题面 URL/Host:Port 正则（复用 challenge/loader.py）
2. 附件就位：<dir>/{attachments,附件}/ 下 zip/tar 自动解压（路径穿越防护）
3. 先验匹配：knowledge/prior_map.yaml 关键词表
4. ground truth 自动发现：<dir>/{flag.txt,answer.txt,ground_truth.txt}
5. 跑完自动评判（AI 评判层）+ 自动经验蒸馏（learning 闭环）
6. profile 含 stages 时自动走多阶段编排
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import typer

from secpt.core.job.models import TaskBudget, TaskSpec

_CHALLENGE_DOC_NAMES = ("test.md", "题目.md", "challenge.md", "README.md")
_ATTACH_DIR_NAMES = ("attachments", "附件")
_GT_NAMES = ("flag.txt", "answer.txt", "ground_truth.txt")
_PRIOR_MAP = Path(__file__).resolve().parents[1] / "knowledge" / "prior_map.yaml"


# ---------------------------------------------------------------------------
# 自动化各步骤（独立容错：单步失败不阻断主流程）
# ---------------------------------------------------------------------------


def _find_doc(target: Path) -> Path:
    if target.is_file():
        return target
    for name in _CHALLENGE_DOC_NAMES:
        cand = target / name
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"{target} 下找不到题面文件（{_CHALLENGE_DOC_NAMES}）")


def _extract_entries(objective: str) -> list[str]:
    """题面 → 入口列表（复用 Challenge 的多组正则）。"""
    try:
        from secpt.challenge.loader import Challenge

        # extract_targets 只依赖 description：绕过 dataclass 构造（避免 path 等必填字段）
        ch = object.__new__(Challenge)
        ch.description = objective
        return ch.extract_targets()
    except Exception as e:
        print(f"[solve] 入口抽取失败（忽略，题面仍完整传入）: {e}")
        return []


def _prepare_attachments(challenge_dir: Path) -> list[str]:
    """解压挑战目录下的附件包到 <dir>/attachments/，返回**绝对路径**列表。

    必须绝对化：engine 的 bash 工作目录在 var/jobs/<id>/ws/，相对路径会漂移
    （author_plus 真实运行暴露：模型读 src/author_plus/... 实际不存在）。
    """
    out_dir = challenge_dir / "attachments"
    out_dir.mkdir(exist_ok=True)
    from secpt.challenge.attachments import AttachmentHandler

    handler = AttachmentHandler(extract_dir=out_dir)
    found = []
    for dname in _ATTACH_DIR_NAMES:
        d = challenge_dir / dname
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if (
                f.suffix.lower() in (".zip", ".tar", ".gz", ".tgz") or "?" in f.name
            ):  # 平台附件常带 ?attname= 查询串
                try:
                    handler.extract(f)
                    found.append(str(out_dir))
                except Exception as e:
                    print(f"[solve] 附件解压失败 {f.name}（忽略）: {e}")
        # 已解压出的源码目录直接可用
        for sub in sorted(out_dir.iterdir()):
            if sub.is_dir():
                found.append(str(sub))
    # 去重保序
    seen, uniq = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _auto_prior(objective: str) -> str | None:
    """关键词表匹配先验；未命中或先验文件未策划 → None（用 profile 默认）。

    只返回 priors 目录真实存在的先验——映射表是愿望清单，缺文件不炸任务。
    """
    try:
        import yaml

        if not _PRIOR_MAP.is_file():
            return None
        table = yaml.safe_load(_PRIOR_MAP.read_text(encoding="utf-8")) or {}
        priors_dir = _PRIOR_MAP.parent / "priors"
        low = objective.lower()
        for prior, keywords in table.items():
            for kw in keywords or []:
                if str(kw).lower() in low:
                    name = str(prior)
                    if (priors_dir / f"{name}.md").is_file():
                        return name
                    print(f"[solve] 先验 {name!r} 未策划（priors/{name}.md 不存在），跳过")
                    return None
    except Exception as e:
        print(f"[solve] 先验匹配失败（忽略）: {e}")
    return None


def _find_ground_truth(challenge_dir: Path) -> Path | None:
    for name in _GT_NAMES:
        cand = challenge_dir / name
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def route_mode(prof, mode: str) -> str:
    """执行模式自动路由（用户不需要知道单链/意图/多阶段）。

    懒升级阶梯：最小充分打法——
      profile 带 stages → stages（用户显式编排）
      其余 → single（planner 无开销，短任务最优）
      single 无成果 → 自动升级 frontier（solve 内失败升级，见 solve()）
    """
    if mode != "auto":
        return mode
    if getattr(prof, "stages", None):
        return "stages"
    return "single"


def solve(
    target: str,
    *,
    profile: str | None = None,
    prior: str | None = None,
    model: str | None = None,
    fallback: str | None = None,
    wall: int = -1,
    steps: int = 0,
    mode: str = "auto",
    judge_enable: bool = True,
    learn_enable: bool = True,
    verbose: bool = False,
    watch_enable: bool = True,
    vpn: bool = False,
) -> int:
    """一键解题（唯一用户入口）。返回退出码：0=完成，1=未完成，2=输入错误。

    两个自动决策（用户无需判断任务类型）：
    - profile 未指定 → 规则关键词 + LLM 兜底自动路由（route_objective）
    - 执行模式 auto → stages(若编排) / single；single 无成果自动升级 frontier
    """
    if vpn:
        # VPN 自举：拉起并验证；沙箱切 host 网络（容器才能走 VPN 路由网段）
        from secpt.runtime import vpn as _vpn

        ok, msg = _vpn.up()
        print(f"[vpn] {msg}")
        if not ok:
            return 2
        os.environ.setdefault("SECPT_SANDBOX_NETWORK", "host")

    target_p = Path(target)
    try:
        doc = _find_doc(target_p)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 2
    # model/fallback 提前解析（CLI 不传 -m 时是 None；judge/蒸馏/frontier 都用这个值，
    # 不提前解析会把 None 传到 client.partition 崩——f242661a 日志里的
    # 'NoneType' object has no attribute 'partition' 即此因）
    from secpt.cli.engine_runner import resolve_models

    model, fallback = resolve_models(model, fallback)

    challenge_dir = (
        doc.parent.resolve()
    )  # 绝对化：附件路径注入 objective 后不能被 bash 工作目录影响
    objective = doc.read_text(encoding="utf-8", errors="replace").strip()

    entries = _extract_entries(objective)
    artifacts = _prepare_attachments(challenge_dir) if challenge_dir.is_dir() else []
    prior_name = prior or _auto_prior(objective)
    gt = _find_ground_truth(challenge_dir)

    # profile 自动路由（用户没指定时；规则优先，LLM 兜底）
    if profile is None or profile == "auto":
        from secpt.core.profile.router import route_objective

        profile = asyncio.run(route_objective(objective))
        print(f"[solve] profile 自动路由 → {profile}")

    job_id = uuid.uuid4().hex[:8]
    spec = TaskSpec(
        job_id=job_id,
        task_type=profile,
        objective=objective,
        prior=prior_name,
        artifacts=artifacts,
        entries=entries,
        budget=TaskBudget(
            steps=steps,
            # --wall 0 = 显式不限（-1 标记）；-1 = 未指定（用 profile）；>0 = 该秒数
            wall_s=-1.0 if wall == 0 else (float(wall) if wall > 0 else 0.0),
        ),
    )

    print(f"[solve] job={job_id} profile={profile} prior={prior_name or '(profile 默认)'}")
    if not watch_enable:
        print(f"[solve] 交互日志已关闭（--no-watch）；事后回看: secpt watch {job_id} --tail 100")
    print(
        f"[solve] 入口 {len(entries)} 个，附件路径 {len(artifacts)} 个，"
        f"ground truth: {'有' if gt else '无'}"
    )

    from secpt.core.job.store import JobStore
    from secpt.core.orchestrator import dag as _dag

    prof = _load_profile(profile)
    run_mode = route_mode(prof, mode)

    async def _run() -> dict:
        store = JobStore()
        if run_mode == "stages":
            from secpt.core.orchestrator.stages import run_stages

            return await run_stages(
                spec, prof, store=store, model_primary=model, model_fallback=fallback
            )
        return await _dag.run_task(
            spec, store=store, use_orchestrator=False, model_primary=model, model_fallback=fallback
        )

    import threading
    import time as _time

    from secpt.cli.watch import render_lines
    from secpt.core.job.resume import jobs_dir

    _stop_watch = threading.Event()

    def _follow_log() -> None:
        """内嵌实时交互日志：跟随 events.jsonl 渲染到人读视图（同终端直接可见）。"""
        path = jobs_dir() / job_id / "events.jsonl"
        start = 0
        while not _stop_watch.is_set():
            if path.exists():
                try:
                    lines, start = render_lines(path, start)
                    for line in lines:
                        print(line, flush=True)
                except OSError:
                    pass
            _time.sleep(0.8)

    _watcher = threading.Thread(target=_follow_log, daemon=True)
    if watch_enable:
        _watcher.start()

    try:
        summary = asyncio.run(_run())
    finally:
        _stop_watch.set()
        if watch_enable:
            _watcher.join(timeout=2)
    flags = summary.get("flags") or []
    flag = flags[0] if flags else None
    print(
        f"\n[solve] 运行结束：mode={run_mode} wall={summary.get('wall_s')}s flag={flag or '未拿到'}"
    )

    # 失败自动升级：single 无成果 → frontier 意图驱动继续（懒升级阶梯）
    if run_mode == "single" and not flag and mode == "auto":
        print("[solve] 单链未拿到成果，自动升级到意图驱动（frontier）继续...")
        from secpt.core.orchestrator.frontier import run_frontier

        retry_note = (
            f"\n\n【首轮单链尝试未成功】已用 {summary.get('wall_s')}s。"
            "请换技术路线深入，不要原样重跑。"
        )
        summary2 = asyncio.run(
            run_frontier(
                objective + retry_note, profile=profile, base_job_id=job_id, llm=None, model=model
            )
        )
        flags2 = summary2.get("flags") or []
        if flags2:
            flag = flags2[0]
            summary["flags"] = flags2
            summary["wall_s"] = round(
                float(summary.get("wall_s", 0)) + float(summary2.get("wall_s", 0)), 1
            )
        print(
            f"[solve] frontier 续跑：flags={flags2 or '无'} "
            f"chains={len(summary2.get('chains') or [])}"
        )

    # 自动导出可读日志（best-effort）
    try:
        from secpt.cli.watch import export_log

        log_path = export_log(
            job_id,
            Path(os.environ.get("SECPT_VAR", "var")) / "jobs" / job_id / "run.log",
            fmt="txt",
        )
        print(f"[solve] 可读日志已保存: {log_path}")
    except Exception as e:
        print(f"[solve] 日志导出失败（忽略）: {e}")

    # 自动评判（best-effort，失败不阻断）
    verdict_ok = False
    if judge_enable:
        try:
            from secpt.evaluation.verdict import evaluate_job, render

            verdict = asyncio.run(
                evaluate_job(job_id, ground_truth_file=str(gt) if gt else None, model=model)
            )
            print("\n[solve] 评判结果：")
            print(render(verdict))
            verdict_ok = verdict.complete
        except Exception as e:
            print(f"[solve] 评判失败（忽略）: {e}")

    # 自动学习（best-effort）
    if learn_enable:
        try:
            from secpt.learning.distill import distill_job

            exp = asyncio.run(distill_job(job_id, model=model))
            if exp:
                print(f"[solve] 已沉淀经验：{exp.title}")
        except Exception as e:
            print(f"[solve] 经验蒸馏失败（忽略）: {e}")

    return 0 if (flag or verdict_ok) else 1


def _load_profile(name: str):
    from secpt.core.profile.loader import load_profile

    return load_profile(name)


# ---------------------------------------------------------------------------
# typer 入口（app.py 注册）
# ---------------------------------------------------------------------------


def solve_cmd(
    target: str = typer.Argument(..., help="挑战目录或题面文件（test.md/题目.md）"),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="任务 profile（默认自动路由：规则关键词→LLM 兜底）"
    ),
    prior: str | None = typer.Option(None, "--prior", help="先验覆盖（默认自动匹配）"),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="主模型（默认 SECPT_MODEL_PRIMARY 环境变量，缺省 deepseek-chat；支持 provider/model）",
    ),
    fallback: str | None = typer.Option(
        None,
        "--fallback",
        help="回退模型（默认 SECPT_MODEL_FALLBACK 环境变量，缺省 deepseek-reasoner）",
    ),
    wall: int = typer.Option(-1, "--wall", help="墙钟上限秒：-1=profile 默认，0=不限"),
    steps: int = typer.Option(0, "--steps", help="步数上限：0=profile 默认"),
    mode: str = typer.Option(
        "auto",
        "--mode",
        help="执行模式：auto（默认，自动选择+失败升级）/ single / frontier / stages",
    ),
    no_judge: bool = typer.Option(False, "--no-judge", help="跑完不自动评判"),
    no_learn: bool = typer.Option(False, "--no-learn", help="跑完不自动沉淀经验"),
    no_watch: bool = typer.Option(False, "--no-watch", help="不打印实时交互日志（脚本/CI 用）"),
    sandbox: bool = typer.Option(
        True, "--sandbox/--no-sandbox", help="bash 在 Docker 容器内执行（宿主隔离；需镜像）"
    ),
    vpn: bool = typer.Option(
        False, "--vpn", help="任务前自动拉起并验证 VPN（SECPT_VPN_TYPE/CONFIG 配置）"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """一键解题（唯一入口）：任务类型与打法自动选择，启动即自带实时交互日志。"""
    import os as _os

    _os.environ["SECPT_SANDBOX"] = "1" if sandbox else "0"
    code = solve(
        target,
        profile=profile,
        prior=prior,
        model=model,
        fallback=fallback,
        wall=wall,
        steps=steps,
        mode=mode,
        judge_enable=not no_judge,
        learn_enable=not no_learn,
        verbose=verbose,
        watch_enable=not no_watch,
        vpn=vpn,
    )
    raise typer.Exit(code)


def learn_cmd(
    job_id: str | None = typer.Argument(None, help="job_id（缺省=最近一个）"),
    model: str = typer.Option("deepseek-chat", "--model", "-m"),
) -> None:
    """手动触发经验蒸馏（learning 闭环补漏）。"""
    from secpt.core.job.store import JobStore
    from secpt.learning.distill import distill_job

    if job_id is None:
        rows = JobStore().list_jobs(limit=1)
        if not rows:
            print("Error: 无历史 job")
            raise typer.Exit(2)
        job_id = rows[0].job_id
    exp = asyncio.run(distill_job(job_id, model=model))
    if exp:
        print(f"已沉淀经验：{exp.title}（technique={exp.technique}, outcome={exp.outcome}）")
    else:
        print("无经验可沉淀（无 transcript / LLM 输出不可解析 / 指纹重复）")
