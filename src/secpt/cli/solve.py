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
import contextlib
import json
import os
import sys
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


_ATTACH_URL_RE = None


def _download_attachment_urls(challenge_dir: Path, objective: str) -> int:
    """题面里的附件 URL 自动下载到 <dir>/附件/（按文件名缓存，不重下）。

    CTF 平台常规：附件是 zip URL（adworld/question_attachment、.zip?attname= 等）。
    下不了（网络/鉴权）不阻断——引擎仍可按 URL 自行处理。
    """
    import re
    import urllib.request

    urls = re.findall(
        r"https?://[^\s\"')]+?(?:\.zip[^\s\"')]*|question_attachment/[^\s\"')]+)", objective
    )
    if not urls:
        return 0
    dest_dir = challenge_dir / "附件"
    dest_dir.mkdir(exist_ok=True)
    n = 0
    for url in dict.fromkeys(urls):  # 去重保序
        # 落盘名净化：剥 "?attname=" 查询串 + 替换 Windows 禁止字形（跨平台安全）
        from secpt.challenge.attachments import sanitize_filename

        name = sanitize_filename(url.split("/")[-1]) or f"attachment-{n}.zip"
        dest = dest_dir / name
        if dest.is_file() and dest.stat().st_size > 0:
            n += 1
            continue
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                dest.write_bytes(resp.read())
            print(f"[solve] 附件已下载: {name} ({dest.stat().st_size // 1024}KB)")
            n += 1
        except Exception as e:
            print(f"[solve] 附件下载失败（忽略，可由引擎自行处理）: {e}")
    return n


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


_PRIOR_SEM_MIN = 0.45  # 先验语义命中阈值（比召回型阈值严：先验是打法级注入，宁缺毋滥）


def _semantic_prior(low: str, table: dict, priors_dir: Path) -> str | None:
    """先验语义兜底（关键词脆弱的互补）：objective 与（先验名+关键词）文档的
    嵌入余弦取最高且过阈值——"逻辑缺陷"能命中语义相近的先验，而非逐字撞词。
    嵌入不可用（未装/未启用）→ None（静默回退旧行为，绝不阻断）。"""
    try:
        from secpt.knowledge import embedding as _emb

        docs: list[tuple[str, str]] = []
        for prior, keywords in table.items():
            name = str(prior)
            if not (priors_dir / f"{name}.md").is_file():
                continue
            docs.append((name, f"{name} " + " ".join(str(k) for k in keywords or [])))
        if not docs:
            return None
        vecs = _emb.embed_cached([low] + [d for _n, d in docs])
        if vecs is None:
            return None
        qv = vecs[0]
        best_name, best_score = "", 0.0
        for (name, _d), v in zip(docs, vecs[1:], strict=True):
            score = _emb.cosine(qv, v)
            if score > best_score:
                best_name, best_score = name, score
        if best_score >= _PRIOR_SEM_MIN:
            print(f"[solve] 先验语义匹配 → {best_name}（score={best_score:.3f}）")
            return best_name
    except Exception:
        return None
    return None


def _auto_prior(objective: str) -> str | None:
    """先验匹配：关键词子串优先（确定性），未命中走语义兜底；都未命中 → None。

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
        return _semantic_prior(low, table, priors_dir)
    except Exception as e:
        print(f"[solve] 先验匹配失败（忽略）: {e}")
    return None


def _find_ground_truth(challenge_dir: Path) -> Path | None:
    for name in _GT_NAMES:
        cand = challenge_dir / name
        if cand.is_file():
            return cand
    return None


def _make_cancel_handler(job_id: str):
    """solve 前台进程的 SIGTERM/SIGHUP 处理器（复评修复）。

    实测事故：终端关闭（SIGHUP）让跑分 run 无痕直死——1.5 小时日志里没有任何
    取消/中断记录。本处理器保证三件事：① 落盘一条 status_change(cancelled)
    事件（事后可复盘）② 打印 resume 指引 ③ 退出码 130。
    不追求完整优雅取消（编排层协作取消链见 core/job/worker._CancelHook，
    由 submit/forever 池路径使用）。
    """

    def _on_cancel(signum, _frame):
        try:
            from secpt.core.job.event import EventSink
            from secpt.core.job.resume import jobs_dir

            EventSink(job_id, jobs_dir()).status_change("cancelled")
        except Exception:
            pass
        print(
            f"\n[solve] 收到信号 {signum}（终端关闭/主动中断）——已中止并落盘。"
            f"断点保留，恢复：secpt resume {job_id}",
            file=sys.stderr,
        )
        raise SystemExit(130)

    return _on_cancel


def _feedback_experiences(job_id: str, outcome: bool) -> int:
    """C 反馈闭环：把本任务注入过的经验按最终成败回写（best-effort）。

    扫描 <job_id>*/experiences_injected.json——覆盖 stages（-<stage>）与
    frontier（-i…）子 job 各自的注入记录；同一条经验多子 job 命中时按任务
    总结果只回写一次（去重）。任何失败静默（learning 绝不打断主链路）。
    返回回写条数。
    """
    try:
        from secpt.core.job.resume import jobs_dir
        from secpt.learning.store import ExperienceJournal

        jr = ExperienceJournal()
        seen: set[str] = set()
        n = 0
        for p in jobs_dir().glob(f"{job_id}*/experiences_injected.json"):
            try:
                ids = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for eid in ids if isinstance(ids, list) else []:
                eid = str(eid)
                if eid and eid not in seen:
                    seen.add(eid)
                    if jr.record_usage(eid, outcome):
                        n += 1
        return n
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 主流程（S6 模式收敛：唯一执行编排 = frontier；benchmark → campaign）
# ---------------------------------------------------------------------------


def solve(
    target: str,
    *,
    profile: str | None = None,
    prior: str | None = None,
    model: str | None = None,
    fallback: str | None = None,
    wall: int = -1,
    steps: int = 0,
    judge_enable: bool = True,
    learn_enable: bool = True,
    verbose: bool = False,
    watch_enable: bool = True,
    vpn: bool = False,
) -> int:
    """一键解题（唯一用户入口）。返回退出码：0=完成，1=未完成，2=输入错误。

    S6 模式收敛：执行编排只有一条路——frontier 意图驱动（stages 折叠为
    线性意图链，见 core/orchestrator/dag.py）；benchmark profile 走 campaign
    （平台协议层）。profile 未指定 → 规则关键词 + LLM 兜底自动路由。
    """
    if vpn:
        # VPN 自举：拉起并验证；沙箱切 host 网络（容器才能走 VPN 路由网段）
        from secpt.runtime import vpn as _vpn

        ok, msg = _vpn.up()
        print(f"[vpn] {msg}")
        if not ok:
            # VPN 未连通是瞬时/可恢复故障（网络抖动、建链慢）：返回 1 让 entrypoint
            # 重启续跑；rc=2 是"平台终局"语义，会把 8h 任务永久停掉
            return 1
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

    # 凭证预检：题面引用的环境变量必须在（tsec-run-1129 教训：BENCHMARK_TOKEN 为空，
    # agent 烧 75 步逆向猜 token + 扫内网 + 错把本地靶场当题目）。
    # 匹配 $VAR / ${VAR} / "环境变量中读取（VAR）" 三种提法；缺失即报错退出。
    import re as _re

    _mentioned = set(_re.findall(r"\$\{?([A-Z][A-Z0-9_]{3,})\}?", objective))
    _mentioned |= set(_re.findall(r"环境变量中读取[（(]([A-Z][A-Z0-9_]{3,})[）)]", objective))
    _skip = {"SECPT_FAKE_LLM"}  # 系统自有开关，不视作任务凭证
    _missing = sorted(v for v in _mentioned if v not in _skip and not os.environ.get(v))
    if _missing:
        print("Error: 题面引用的环境变量未设置，已中止（避免无凭证空跑）：")
        for v in _missing:
            print(f"  export {v}=<值>    # 或写入 .env")
        return 2

    _download_attachment_urls(challenge_dir, objective)
    entries = _extract_entries(objective)
    artifacts = _prepare_attachments(challenge_dir) if challenge_dir.is_dir() else []
    prior_name = prior or _auto_prior(objective)
    gt = _find_ground_truth(challenge_dir)

    # 协议闸口（L0 唯一保留的分类决策）：benchmark 是外部平台协议（拉题/容器/
    # 结算/账本纪律），必须开跑前判定；判定走确定性规则（tsec/跑分/unique_code
    # 等平台关键词），不烧 LLM。
    # 其余——角色/安全场景的选择、经验（先验）的选择——不是协议，是任务理解的
    # 一部分：由 Commander 首轮唤醒从角色目录中选定并赋予操作员（可逐任务覆盖）。
    if profile in (None, "auto"):
        from secpt.core.profile.router import _match_rules

        matched = _match_rules(objective)
        profile = "benchmark" if "benchmark" in matched else "auto"
        if profile == "benchmark":
            print("[solve] 协议闸口：命中 benchmark 平台协议（规则）→ 战役跑分")
        else:
            print("[solve] 角色/场景/经验选择：交由指挥官首轮唤醒完成")
    else:
        print(f"[solve] 角色显式指定：{profile}（指挥官不再改）")

    _goals_why = (
        "benchmark 豁免（程序化协议）"
        if profile == "benchmark"
        else f"SECPT_GOALS={os.environ.get('SECPT_GOALS', '1')}"
    )
    print(f"[solve] 目标拆解：交由指挥官首轮唤醒完成（{_goals_why}）")

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
    from secpt.core.orchestrator.benchmark import BenchmarkEnded, BenchmarkUnavailable

    prof = _load_profile(profile)
    if getattr(prof, "roe", None) is not None and prof.roe.mode == "authorized":
        _throttle = f"，节速 {prof.roe.throttle_ms}ms/步" if prof.roe.throttle_ms > 0 else ""
        print(
            f"[ROE] 授权实战模式（authorized）{_throttle}——仅限授权目标，"
            f"全部操作落审计日志，遵守 profile.constraints 边界"
        )

    async def _run() -> dict:
        store = JobStore()
        # benchmark 评测：并行跑分器（3 题并发 × 意图级止损 × 自动多轮战役）；
        # 其余：统一走 dag.run_task → frontier 意图编排（唯一执行模式，S6）
        if prof.name == "benchmark":
            from secpt.core.orchestrator.benchmark import (
                BenchmarkUnavailable,
                run_benchmark_campaign,
            )

            try:
                # A2：战役多轮——平台实况驱动，未完成题带 digest 自动进入下一轮
                return await run_benchmark_campaign(spec, llm=None, model=model)
            except BenchmarkUnavailable:
                raise  # 平台级失败（任务结束/网络）：快速失败不降级，由 solve 顶层清晰提示
            # 注意：不再兜底 RuntimeError 回退单链。战役内部任何异常都应向上抛，
            # 由 entrypoint 重启续跑；静默降级到 DAG 会绕过平台协议，甚至把非平台
            # flag 当成功（rc=0）导致任务提前结束。
        return await _dag.run_task(
            spec, store=store, llm=None, model_primary=model, model_fallback=fallback
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

    # P2：前台信号收尾（SIGTERM/SIGHUP → 落盘 cancelled + resume 指引 + 130）
    import signal as _signal

    _cancel = _make_cancel_handler(job_id)
    for _sig in (_signal.SIGTERM, getattr(_signal, "SIGHUP", None)):
        if _sig is not None:
            with contextlib.suppress(ValueError, OSError):
                _signal.signal(_sig, _cancel)  # 非主线程/平台不支持 → 保持默认行为

    try:
        summary = asyncio.run(_run())
    except BenchmarkEnded as e:
        # 平台任务已结束（409/finished）：终局，entrypoint 见 rc=2 不再重启
        print(f"\n[solve] Error: {e}")
        return 2
    except BenchmarkUnavailable as e:
        # 瞬时不可达（网络/VPN 抖动、5xx）：不是终局——返回 1 让 entrypoint 重启续跑。
        # 混用 rc=2 会让一次网络抖动把 8h 中的整场跑分永久停掉（OOM 重启同理）。
        print(f"\n[solve] 平台暂时不可达: {e}")
        return 1
    except KeyboardInterrupt:
        # Ctrl-C：与信号处理器同口径（落盘 + 指引）；_cancel 正常会 raise 130
        _cancel(2, None)
        return 1
    except Exception as e:
        # 未分类异常（含战役内部 RuntimeError/编程错误）：落 traceback 并以 rc=1
        # 退出，交给 entrypoint 重启续跑；绝不能裸穿成 typer traceback 或静默结束。
        import traceback

        print(f"\n[solve] 未处理异常: {e}")
        traceback.print_exc()
        return 1
    finally:
        _stop_watch.set()
        if watch_enable:
            _watcher.join(timeout=2)
    flags = summary.get("flags") or []
    flag = flags[0] if flags else None
    print(
        f"\n[solve] 运行结束：mode={summary.get('mode', 'frontier')} wall={summary.get('wall_s')}s flag={flag or '未拿到'}"
    )
    if summary.get("all_completed"):
        # 平台已无未完成题：正常终局，返回 0（entrypoint 不再重启空转）
        print("[solve] 平台题目已全部完成，正常退出")
        return 0

    # S6：失败升级路径已随单链模式退役——唯一编排 frontier 内部自带
    # 意图复核/RESET/升级强模型，编排层不再做"单链→frontier"二次升级。

    # 自动导出可读日志（best-effort）：job 目录 run.log（归档副本）+ logs/<job_id>.log（集中存放）
    try:
        from secpt.cli.watch import export_log

        var_dir = Path(os.environ.get("SECPT_VAR", "var"))
        export_log(job_id, var_dir / "jobs" / job_id / "run.log", fmt="txt")
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        log_path = export_log(job_id, logs_dir / f"{job_id}.log", fmt="txt")
        print(f"[solve] 可读日志已保存: {log_path}（另 var/jobs/{job_id}/run.log 归档副本）")
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

    # C 反馈闭环：本任务注入过的经验按最终成败回写（best-effort，静默降级）
    if learn_enable:
        _outcome = bool(flag or verdict_ok)
        _n_fb = _feedback_experiences(job_id, _outcome)
        if _n_fb:
            print(f"[solve] 经验反馈回写：{_n_fb} 条（outcome={'hit' if _outcome else 'miss'}）")

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
    no_judge: bool = typer.Option(False, "--no-judge", help="跑完不自动评判"),
    no_learn: bool = typer.Option(False, "--no-learn", help="跑完不自动沉淀经验"),
    no_watch: bool = typer.Option(False, "--no-watch", help="不打印实时交互日志（脚本/CI 用）"),
    sandbox: bool = typer.Option(
        True, "--sandbox/--no-sandbox", help="bash 在 Docker 容器内执行（宿主隔离；需镜像）"
    ),
    vpn: bool = typer.Option(
        # VPN 总开关（PA 部署化）：SECPT_VPN=1 等效 --vpn，默认关；--no-vpn 可显式压过
        os.environ.get("SECPT_VPN", "0") == "1",
        "--vpn/--no-vpn",
        help="任务前自动拉起并验证 VPN（开关 SECPT_VPN=0/1；连接参数 SECPT_VPN_TYPE/CONFIG）",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """一键解题（唯一入口）：唯一执行编排 frontier（benchmark 走 campaign），
    启动即自带实时交互日志。"""
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
        judge_enable=not no_judge,
        learn_enable=not no_learn,
        verbose=verbose,
        watch_enable=not no_watch,
        vpn=vpn,
    )
    raise typer.Exit(code)
