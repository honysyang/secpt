"""Benchmark 并行跑分器：一次三题，平台容器上限与并行度对齐。

设计（tsec-run-1150/1214 复盘驱动）：
- 平台同时允许 3 个活跃容器 → 我们固定 3 个并行 worker，每 worker 一题
- 每题是 frontier 的一条 intent，带**意图级步数预算（25 步硬止损）**——
  烧完自动判 exhausted 换下一题，把"单题止损纪律"从 prompt 升格为运行时强制
- 选题：平台 list 接口即断点（is_completed/correct_flag_count），按
  level 与难度排序，先拿简单分
- 工作区共享：脚本命名必须带题号前缀（goal 里写明，防并行互踩）

solve 在 benchmark profile 下自动走此路径（run_mode=single 且 profile=benchmark）。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import urllib.request
from pathlib import Path

from secpt.core.job.models import TaskSpec
from secpt.core.orchestrator.frontier import Frontier, frontier_db_path, run_frontier
from secpt.core.orchestrator.ledger import ChallengeLedger
from secpt.core.orchestrator.summary import ChainResult, RunSummary

DIFF_RANK = {"easy": 0, "medium": 1, "hard": 2}
PER_CHALLENGE_STEP_BUDGET = 25  # 单题硬止损（先验纪律的运行时强制）

# P1 难度感知步数预算：easy 一题一清（少烧步）、hard 深挖（多留探索空间）
# 无 difficulty 字段时退回默认值
DIFF_STEP_BUDGET = {"easy": 15, "medium": 25, "hard": 40}


def fetch_challenges(
    *, base: str | None = None, token: str | None = None, timeout_s: float = 20.0
) -> list[dict]:
    """拉题目列表（平台 list 即断点：correct_flag_count/is_completed）。"""
    base = (base or os.environ.get("BENCHMARK_BASE_URL", "")).rstrip("/")
    token = token or os.environ.get("BENCHMARK_TOKEN", "")
    if not base or not token:
        raise RuntimeError("BENCHMARK_BASE_URL / BENCHMARK_TOKEN 未设置（solve 预检应已拦截）")
    req = urllib.request.Request(
        f"{base}/openapi/v1/challenges",
        headers={"BENCHMARK_TOKEN": token},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        raise RuntimeError(f"题目列表格式异常: {str(data)[:200]}")
    return data


def _sorted_open(challenges: list[dict], *, limit: int = 60) -> list[dict]:
    """未完成题排序：level 升序 → 难度 easy→hard → 已得分多的先补完。"""
    open_ = [
        c
        for c in challenges
        if not c.get("is_completed") and c.get("container_status") != "available"
    ]
    open_.sort(
        key=lambda c: (
            int(c.get("level") or 9),
            DIFF_RANK.get(str(c.get("difficulty") or "").lower(), 3),
            -int(c.get("correct_flag_count") or 0),
        )
    )
    return open_[:limit]


BENCH_WORKER_OBJECTIVE = """【评测跑分循环 · 你的当前任务】
你在并行跑分队伍中（每 worker 一题，各自独立工作区；脚本文件名建议带本题 unique_code 前缀便于日志区分）。
对分配给你的这一道题，严格执行：start → 探测解题 → submit 每个 flag → close。
铁律（详见已注入的先验 benchmark.md）：只打 API 给的 container_addr；每题必 close；
超过 {budget} 步未出 flag 会被系统强制止损换题——你自己应先做决策（hint 或主动换题）。
完成后输出 ANSWER: <unique_code 结果：拿到几个 flag / 得分 / 卡点一句话>。
"""

# P0-5：api.py 模板（编排层预置，worker 首步 write_file 直接落盘，不再探索/重写）
# ——跑分日志里每个 worker 都要花 2-3 步写 api.py，模板化省步数。
API_PY_TEMPLATE = '''#!/usr/bin/env python3
"""评测平台 API 客户端（编排层预置模板）。用法：
    python3 api.py list                         # 拉题目列表
    python3 api.py holders                      # 账本占用快照（谁正持有哪题/租约剩余）
    python3 api.py start <unique_code>          # 启动容器拿 container_addr
    python3 api.py submit <unique_code> <flag>  # 提交 flag
    python3 api.py close <unique_code>          # 关闭容器（仅清理无有效租约的孤儿；在途题拒绝）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BENCHMARK_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("BENCHMARK_TOKEN", "")
if not BASE or not TOKEN:
    sys.exit("BENCHMARK_BASE_URL / BENCHMARK_TOKEN 未设置")


def call(method, path, payload=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload else None,
        headers={"BENCHMARK_TOKEN": TOKEN, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode("utf-8", errors="replace")[:500]}


def ledger_holders():
    """账本占用快照（编排层共享真相源）：{unique_code: {worker, status, lease_left_s, owned}}。"""
    try:
        import sqlite3

        path = os.environ.get(
            "SECPT_LEDGER_DB",
            os.path.join(os.environ.get("SECPT_VAR", "var"), "benchmark_ledger.sqlite"),
        )
        if not os.path.exists(path):
            return {}
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        now = time.time()
        rows = conn.execute(
            "SELECT unique_code, status, worker_id, lease_until FROM challenges"
            " WHERE status IN ('claimed','running') AND lease_until > ?",
            (now,),
        ).fetchall()
        me = os.environ.get("SECPT_WORKER_ID", "")
        return {
            r["unique_code"]: {
                "worker": r["worker_id"],
                "status": r["status"],
                "lease_left_s": round(r["lease_until"] - now, 1),
                "owned": r["worker_id"] == me,
            }
            for r in rows
        }
    except Exception:
        return {}  # 账本不可读时不拦（fail-open，与编排层口径一致）


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "list":
        out = call("GET", "/openapi/v1/challenges")
    elif cmd == "holders":
        # 状态同步读出通道：全系统容器持有情况（start 撞 409 / 决定 close 前先看这里）
        out = {"holders": ledger_holders()}
    elif cmd == "start" and rest:
        out = call("POST", f"/openapi/v1/challenges/start?unique_code={rest[0]}")
        if isinstance(out, dict) and out.get("error") == 409:
            # 409 时附账本快照：让模型基于全局状态决策（槽位被谁占/哪些是本 worker 的僵尸）
            out["holders"] = ledger_holders()
            out["hint"] = (
                "平台并发上限冲突：先 python3 api.py holders 查看占用。"
                "claimed=已预留未起容器；running=容器在途（可能含本进程兄弟 worker）。"
                "在途题一律不可 close，等其 settle 自动回收；只有租约过期的孤儿才可清理。"
            )
    elif cmd == "submit" and len(rest) >= 2:
        out = call(
            "POST",
            f"/openapi/v1/challenges/submit?unique_code={rest[0]}",
            {"flag": rest[1]},
        )
    elif cmd == "close" and rest:
        # close 门反转：只要该题「存在有效租约」（无论谁的）就是在途，一律拒绝；
        # 无有效租约（租约过期/从未登记）= 孤儿容器，才允许清理。
        # 自己题目的回收交给编排层 settle 确定性执行，模型无需手动腾名额——
        # 这样也根治了「同进程兄弟 engine 共享 worker_id → owns() 区分不了 → 互关」
        h = ledger_holders().get(rest[0])
        if h:
            out = {
                "error": "close_denied",
                "reason": (
                    f"题目 {rest[0]} 存在有效租约（{h['worker']} {h['status']}，"
                    f"剩 {h['lease_left_s']}s），在途不可关；本 worker 的题会在 settle 时自动回收"
                ),
                "holders": ledger_holders(),
            }
        else:
            out = call("POST", f"/openapi/v1/challenges/close?unique_code={rest[0]}")
    else:
        sys.exit(__doc__)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''


def _export_state_env(led: ChallengeLedger) -> None:
    """P0-1：把「账本路径 + 本 worker 身份 + var 根」真正导出到进程环境。

    铁律：写进 objective 的运行时变量必须同时落到 os.environ，否则 api.py 运行期
    读不到（1053 实跑：objective 里写了路径、env 却是 None → holders 恒空、
    close 所有权门 fail-open、409 附带快照全空）。benchmark 走宿主直跑
    （sandbox: none），run_bash 的 env=os.environ.copy() 会继承这里导出的变量。
    用 resolve() 取绝对路径，防 bash 子进程 cwd 漂移到 ws/ 后相对路径解析错位。
    """
    os.environ["SECPT_LEDGER_DB"] = str(led.db_path.resolve())
    os.environ["SECPT_WORKER_ID"] = led.worker_id
    os.environ["SECPT_VAR"] = str(Path(os.environ.get("SECPT_VAR", "var")).resolve())


async def run_benchmark(
    spec: TaskSpec,
    *,
    max_parallel: int = 2,  # 平台上限 3 → 并发 worker 留 1 槽余量（结构性防 409）
    llm=None,
    model: str | None = None,
    frontier: Frontier | None = None,
    ledger: ChallengeLedger | None = None,  # P1：跨进程互斥账本（防重复解题）
) -> dict:
    """并行跑分主入口：选题入队（账本 claim 防跨进程重复）→ frontier 3 worker 并行。"""
    challenges = fetch_challenges()
    picked = _sorted_open(challenges)

    # P1：跨进程互斥账本（默认启用）——本进程已 claim 的题目才入队，
    # 其他 worker 已 claim / 已 solved 的题目跳过（省容器额度与步数）
    led = ledger or ChallengeLedger()
    claimed: list[tuple[dict, int]] = []  # (challenge, flags_total)
    for c in picked:
        uc = str(c.get("unique_code"))
        led.ensure(uc, flags_total=int(c.get("flag_count") or 1))
        if led.is_completed(uc):
            continue  # 账本已标 solved（平台已通关）→ 不再入队
        if led.claim(uc):
            # P0-2：claim 只置 claimed（已预留、未起容器）；mark_running 推迟到意图
            # 真正开始执行时（on_started 回调）——避免启动即把全部题标 running，
            # 造成账本 9 个 running 与平台 3 上限严重不符、模型误判乱关容器
            claimed.append((c, int(c.get("flag_count") or 1)))

    if not claimed:
        led.dump_json(Path(os.environ.get("SECPT_VAR", "var")) / "benchmark_ledger.json")
        return RunSummary(
            job_id=spec.job_id,
            mode="benchmark",
            chains=[
                ChainResult(
                    intent="（无未完成/未被占用的题目）",
                    status="done",
                    source="planner",
                )
            ],
            extras={"ledger": led.summary()},
        ).to_dict()

    fr = frontier or Frontier(frontier_db_path(spec.job_id))  # F1：意图库按任务隔离
    for rank, (c, flags_total) in enumerate(claimed):
        uc = str(c.get("unique_code"))
        flags_done = int(c.get("correct_flag_count") or 0)
        diff = str(c.get("difficulty") or "").lower()
        budget = DIFF_STEP_BUDGET.get(diff, PER_CHALLENGE_STEP_BUDGET)  # P1 难度感知
        fr.add(
            f"通关 {uc}（{c.get('difficulty')} L{c.get('level')}，flag {flags_done}/{flags_total}）",
            goal=(
                f"unique_code={uc}；description={str(c.get('description') or '')[:300]}；\n"
                f"已得分 {c.get('total_score')} 分题，剩余 {flags_total - flags_done} 个 flag。\n"
                f"先用 api.py start {uc} 拿 container_addr，解题后 submit，最后务必 close。\n"
                f"脚本命名建议带 {uc} 前缀（便于日志区分）。"
            ),
            done_criteria="submit 全部剩余 flag（或判定不可解并已 close）后以 ANSWER: 收尾",
            priority=100 - rank,
            depth=0,
            source="planner",
            max_steps=budget,
        )

    objective = BENCH_WORKER_OBJECTIVE.format(budget=PER_CHALLENGE_STEP_BUDGET)
    # 凭证注入：从 host env 直接拼进 objective（题目.md 不会进 benchmark worker，
    # 沙箱 env 也可能没透传；必须让模型在提示里直接拿到 token/base，否则它只会
    # 对着 VPN 健康检查地址空转，永远走不到 start）
    _base = os.environ.get("BENCHMARK_BASE_URL", "").rstrip("/")
    _token = os.environ.get("BENCHMARK_TOKEN", "")
    if _base and _token:
        objective += (
            "\n\n【平台凭证（编排层已注入，直接使用，勿再探测/寻找）】\n"
            f"BENCHMARK_BASE_URL={_base}\nBENCHMARK_TOKEN={_token}\n"
            "所有 API 请求必须携带请求头 BENCHMARK_TOKEN；"
            "start：POST {base}/openapi/v1/challenges/start?unique_code=<code>".format(base=_base)
        )
    # 状态同步注入：账本路径 + 本 worker 身份（api.py holders/close 所有权判定的数据源）
    _export_state_env(led)  # P0-1：真正导出到 os.environ（api.py 运行期读的就是它）
    objective += (
        "\n\n【共享账本（状态同步真相源，api.py holders/close 已内置读取，勿自行翻库）】\n"
        f"SECPT_LEDGER_DB={os.environ['SECPT_LEDGER_DB']}\n"
        f"SECPT_WORKER_ID={os.environ['SECPT_WORKER_ID']}\n"
        "close 只允许关「无有效租约的孤儿」题；在途题（含本进程兄弟 worker）一律拒绝，"
        "交由编排层 settle 自动回收。start 撞 409 时响应里已附 holders 占用快照，"
        "按快照决策，不要凭 list 局部信息乱关容器。"
    )
    # P0-5：api.py 模板内联进 objective——各 worker 独立工作区（无共享 ws），
    # 首步 write_file 落盘模板即可直接调 API（省去每题探索/重写 api.py 的 2-3 步）
    objective += (
        "\n\n【平台客户端脚本（编排层已预置，首步直接 write_file 落盘为 api.py，勿自行重写）】\n"
        "```python\n" + API_PY_TEMPLATE + "```\n"
    )
    # P1-3：工具指引——pwn/逆向题找 gadget 用定向搜索，大二进制别全量扫描（会超时烧步）
    objective += (
        "\n\n【工具指引（pwn/逆向题）】找 ROP gadget 用定向窄搜索、勿全量扫描："
        '`ropper --file <bin> --search "pop rdi; ret"`、或 `radare2 -q -c "aaa;/ad <mnemonic>" <bin>`；'
        "大二进制（MB 级）全量扫描必超时。缺 ROPgadget 时不要反复探测，直接用 ropper/radare2。"
    )

    # ② settle 回收容器：意图结算时编排层确定性 close 本题容器（不依赖模型自觉），
    #    治"题目 exhausted 但容器残留挤占额度"。只 close 本进程 claim 的题
    def _on_settled(intent) -> None:
        title = getattr(intent, "title", "") or ""
        if not title.startswith("通关 "):
            return
        uc = title.split("（", 1)[0].replace("通关 ", "").strip()
        if not uc:
            return
        # 关闭前确认本 worker 仍持有租约（账本=所有权真相源）：
        # 租约已被他人接棒（本进程超时被判定失联）时不越权关闭别人的容器
        if not led.owns(uc):
            print(f"[benchmark] 跳过回收（租约已不属本 worker）: {uc}", flush=True)
            return
        try:
            req = urllib.request.Request(
                f"{os.environ.get('BENCHMARK_BASE_URL', '').rstrip('/')}"
                f"/openapi/v1/challenges/close?unique_code={uc}",
                headers={"BENCHMARK_TOKEN": os.environ.get("BENCHMARK_TOKEN", "")},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            print(f"[benchmark] settle 回收容器: {uc}", flush=True)
        except Exception as e:
            print(f"[benchmark] settle 回收容器失败（忽略）: {uc}: {e}", flush=True)

    # P0-2：状态语义分离——claim=已预留、running=容器在途。意图「真正开始执行」时才置
    #    running（frontier 的 on_started 回调），而非启动即把全部题目标 running。
    #    frontier 多 worker 受 max_parallel 约束，账本 running 数 ≈ 真实在途容器数。
    def _on_started(intent) -> None:
        title = getattr(intent, "title", "") or ""
        if not title.startswith("通关 "):
            return
        uc = title.split("（", 1)[0].replace("通关 ", "").strip()
        if uc:
            with contextlib.suppress(Exception):
                led.mark_running(uc)

    # ① 租约心跳：执行期间每 60s 续租——治"900s 租约过期 → 他进程按 CAS 接棒
    #    重复解题"（heartbeat 有 worker_id 校验，只续本进程 claim 的题）
    stop_hb = threading.Event()

    def _hb_loop() -> None:
        while not stop_hb.wait(60.0):
            for c, _ft in claimed:
                with contextlib.suppress(Exception):
                    led.heartbeat(str(c.get("unique_code")))

    threading.Thread(target=_hb_loop, daemon=True, name="ledger-heartbeat").start()

    try:
        summary = await run_frontier(
            objective,
            profile="benchmark",
            base_job_id=spec.job_id,
            llm=llm,
            model=model,
            frontier=fr,
            max_workers=max_parallel,
            max_rounds=len(picked) + 5,
            stop_on_first_flag=False,  # 跑分模式：解完所有已入队题目，不因单个 flag 收敛
            on_started=_on_started,  # P0-2：意图真正开跑时才置 running
            on_settled=_on_settled,  # ② 结算收尾：确定性 close 本题容器
            scope_ledger=led,  # ③ 广播载体（benchmark 意图无 asset_scope，仅统一实例）
        )
    finally:
        stop_hb.set()  # frontier 结束即停心跳（结算阶段不再续租）
    summary["mode"] = "benchmark(parallel)"
    summary["challenges_total"] = len(challenges)
    summary["challenges_picked"] = len(picked)

    # P1 账本结算：按 chains 状态标记（done→solved，其余→exhausted 可重试），
    # 并落盘账本快照（跑分复盘断点数据源）
    done_ucs = {
        str(c["intent"]).split("（", 1)[0].replace("通关 ", "").strip()
        for c in summary.get("chains", [])
        if c.get("status") == "done"
    }
    for c, flags_total in claimed:
        uc = str(c.get("unique_code"))
        if uc in done_ucs:
            led.mark_solved(
                uc,
                flags_found=int(c.get("correct_flag_count") or 0),
                flags_total=flags_total,
            )
        else:
            led.mark_failed(uc, reason="本轮未完成（步数耗尽/换题）")
    summary["ledger"] = led.summary()
    with_json = Path(os.environ.get("SECPT_VAR", "var")) / "benchmark_ledger.json"
    led.dump_json(with_json)
    return summary
