"""Benchmark 并行跑分器：一次三题，平台容器上限与并行度对齐。

设计（tsec-run-1150/1214/1323 复盘驱动）：
- 平台同时允许 3 个活跃容器 → 默认 3 个并行 worker（SECPT_BENCHMARK_WORKERS 可调）
- 每题是 frontier 的意图，带**意图级步数预算（按难度 40/60/110）**；多旗题按
  2 旗/段拆意图（A1），段间 asset_scope 串行、逐段结算；≥4 旗追加 verify 意图
- 结算以平台实况为准（fail-closed）；失败题由 campaign 多轮自动重试（A2），
  平台结束/期限到则优雅排空（A3）
- 选题：平台 list 接口即断点（is_completed/correct_flag_count），按
  level 与难度排序，先拿简单分
- 工作区共享：脚本命名必须带题号前缀（goal 里写明，防并行互踩）

solve 在 benchmark profile 下自动走此路径（run_mode=single 且 profile=benchmark）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from secpt.core.job.models import TaskSpec
from secpt.core.orchestrator.frontier import Frontier, frontier_db_path, run_frontier
from secpt.core.orchestrator.ledger import ChallengeLedger
from secpt.core.orchestrator.summary import ChainResult, RunSummary


class BenchmarkUnavailable(RuntimeError):
    """benchmark 平台不可用（任务已结束 / HTTP 错误 / 网络失败）。

    快速失败而非降级：跑分依赖平台协议，回退单链只会对着死任务烧预算。
    solve 顶层捕获本异常 → 清晰提示 + 退出码 2。
    """


class BenchmarkEnded(BenchmarkUnavailable):
    """平台任务已结束（invalid_state / task_not_found）——A3：campaign 应停止而非重试。"""


DIFF_RANK = {"easy": 0, "medium": 1, "hard": 2}
PER_CHALLENGE_STEP_BUDGET = 40  # 单题硬止损兜底（先验纪律的运行时强制）

# P1 难度感知步数预算。取值依据（tsec1-run-1213 / 1323 两轮复盘）：每题固定开销就要
# 6-8 步（api.py/脚手架/容器冷启动 15-30s/首轮探测）；1323 实测 8 道 hard 题（含
# 500 分题 5 道）在 73-77 步撞 80 墙，其中 f2-05 墙后 2-3 步即拿到 golden insight
# （访问码已验证、只差 flag 重建）——墙前顿悟是普遍形态，hard 必须留够探索深度：
# easy 40 / medium 60 / hard 110，多旗题再乘旗数放大（ceil(flags/2) 封顶 4）。
# 无 difficulty 字段时退回默认值
DIFF_STEP_BUDGET = {"easy": 40, "medium": 60, "hard": 110}


# ───────────────────────── A1/B1/C2 意图生成辅助 ─────────────────────────


def uc_from_title(title: str) -> str:
    """从意图标题提取 unique_code（通关/复验两种前缀；兼容带段后缀）。"""
    m = re.search(r"(?:通关|复验)\s+([a-z0-9-]+)", title or "")
    return m.group(1) if m else ""


def chunk_ranges(flags_total: int) -> list[tuple[int, int]]:
    """多旗题分段：(start,end] 旗号区间。≤3 旗单段；≥4 旗按每段 2 旗切。

    1323 实证：b-01(4 旗)/b-02(6 旗) 单意图串行，任何一旗卡住全线停摆。
    分段后每段独立结算：拿到一旗立刻 submit 计分，某段失败不阻塞其余段
    （asset_scope 串行 + 优先级排序，不用硬依赖）。
    """
    n = max(1, int(flags_total))
    if n <= 3:
        return [(1, n)]
    return [(i, min(n, i + 1)) for i in range(1, n + 1, 2)]


_WEB_HINTS = (
    "web",
    "http",
    "登录",
    "门户",
    "接口",
    "上传",
    "waf",
    "xss",
    "sql",
    "ssrf",
    "saml",
    "sso",
    "js",
    "javascript",
    "node",
    "反序列化",
    "越权",
    "注入",
)
_BIN_HINTS = (
    "二进制",
    "固件",
    "逆向",
    "反编译",
    "反汇编",
    "可执行文件",
    "elf",
    "binary",
    "mcu",
    "arm",
    "x86",
    "字节码",
    "vm",
    "密码",
    "加密",
    "crypto",
)

# 面板/数据库/中间件信号 → web_search 装配（c 族"指纹→已知漏洞"的检索出口）。
# 仅在已配置检索后端（TAVILY/BRAVE/SEARXNG）时才挂——没后端时挂了只是死 schema。
_WEBSEARCH_HINTS = (
    "1panel",
    "宝塔",
    "phpmyadmin",
    "adminer",
    "neo4j",
    "图数据库",
    "管理控制台",
    "管理面板",
    "控制面板",
    "jenkins",
    "gitlab",
    "nacos",
    "minio",
    "influxdb",
    "grafana",
    "kibana",
    "weblogic",
    "中间件",
)


_OOB_HINTS = (
    "ssrf",
    "xxe",
    "盲注",
    "blind",
    "外带",
    "oob",
    "dnslog",
    "回调",
    "url 参数",
    "url参数",
    "图片加载",
    "webhook",
)


def tools_for_challenge(description: str) -> str:
    """C2/M4：按题面信号动态启用插件工具（逗号分隔；空=profile 默认）。

    - web 族：http_request/session（结构化请求 + 常驻会话）+ recon_parse（扫描结果结构化）；
    - 含 SSRF/XXE/盲注/外带信号：额外启用 oob（带审计回调监听，盲验证）；
    - 二进制/逆向族：analyze_binary/crypto_pubkey/pwn_checksec + recon_parse + session
      （pty 驱动 gdb 动态调试）；
    - 面板/数据库/中间件信号（_WEBSEARCH_HINTS）：web_search（需已配检索后端，
      否则不挂——c 族"指纹→已知漏洞"的检索出口）；
    - 浏览器：仅 SECPT_BENCH_BROWSER=1 且 web 族且 playwright 可用时启用
      （6 个 browser_* schema 占 token，默认关）。
    只加轻量内置插件，不改变治理链。
    """
    low = (description or "").lower()
    tools: list[str] = []
    is_web = any(k in low for k in _WEB_HINTS)
    if is_web:
        tools += ["http_request", "session", "recon_parse"]
    if any(k in low for k in _BIN_HINTS):
        tools += ["analyze_binary", "crypto_pubkey", "pwn_checksec", "recon_parse", "session"]
    if is_web and any(k in low for k in _OOB_HINTS):
        tools.append("oob")
    if any(k in low for k in _WEBSEARCH_HINTS) and (
        os.environ.get("TAVILY_API_KEY")
        or os.environ.get("BRAVE_API_KEY")
        or os.environ.get("SECPT_SEARCH_URL")
    ):
        tools.append("web_search")
    if is_web and os.environ.get("SECPT_BENCH_BROWSER", "0") == "1":
        import importlib.util

        if importlib.util.find_spec("playwright") is not None:
            tools += [
                "browser_check",
                "browser_navigate",
                "browser_get_content",
                "browser_click",
                "browser_fill",
                "browser_screenshot",
            ]
    return ",".join(dict.fromkeys(tools))


# A3：平台快照短缓存（settle 回调里判断"是否全部通关/是否可关容器"用，
# on_settled 是串行 to_thread 调用，一把锁足够）
_PLATFORM_CACHE: dict[str, object] = {"ts": 0.0, "data": None}
_PLATFORM_LOCK = threading.Lock()


def _platform_snapshot(force: bool = False, ttl: float = 5.0) -> dict[str, dict]:
    """uc→challenge 的短 TTL 快照。平台不可达时抛 BenchmarkUnavailable。"""
    now = time.time()
    with _PLATFORM_LOCK:
        if (
            not force
            and _PLATFORM_CACHE["data"] is not None
            and now - float(_PLATFORM_CACHE["ts"]) < ttl
        ):
            return _PLATFORM_CACHE["data"]  # type: ignore[return-value]
    data = {str(c.get("unique_code")): c for c in fetch_challenges()}
    with _PLATFORM_LOCK:
        _PLATFORM_CACHE["ts"] = now
        _PLATFORM_CACHE["data"] = data
    return data


def _close_challenge(uc: str) -> bool:
    """确定性 close（编排层专用，绕过模型侧 close 门）。"""
    try:
        req = urllib.request.Request(
            f"{os.environ.get('BENCHMARK_BASE_URL', '').rstrip('/')}"
            f"/openapi/v1/challenges/close?unique_code={uc}",
            headers={"BENCHMARK_TOKEN": os.environ.get("BENCHMARK_TOKEN", "")},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _playbook_section(uc: str) -> str:
    """B2：族级打法注入（自动蒸馏，best-effort）。"""
    try:
        from secpt.learning.playbook import playbook_for

        return playbook_for(uc)
    except Exception:
        return ""


def _platform_error(e: urllib.error.HTTPError) -> BenchmarkUnavailable:
    """HTTPError → 可行动的 BenchmarkUnavailable（解析平台 JSON 错误体）。"""
    body = ""
    with contextlib.suppress(Exception):
        body = e.read().decode("utf-8", errors="replace")[:300]
    detail = ""
    with contextlib.suppress(Exception):
        d = json.loads(body)
        detail = f"{d.get('code', '')}: {d.get('message', '')}".strip(": ")
    if "finished" in body or "invalid_state" in body or "task_not_found" in body:
        return BenchmarkEnded(
            f"平台任务已结束（HTTP {e.code} {detail}）——本次跑分无法继续（所有端点都会 409）。\n"
            "  处理：① 到平台创建**新任务**，更新 .env 的 BENCHMARK_TOKEN 与 vpn/ 配置；\n"
            "  ② 若新任务复用相同题目编码（g-*）：旧账本会把历史 solved 当断点、running 当在途，\n"
            "     建议归档 mv var/benchmark_ledger.sqlite var/benchmark_ledger.old.bak，\n"
            "     或设置 BENCHMARK_TASK_ID=<新任务id> 让账本按任务隔离；\n"
            "  ③ 重跑同一条 solve 命令。"
        )
    return BenchmarkUnavailable(f"平台返回 HTTP {e.code} {detail or body}")


def fetch_challenges(
    *, base: str | None = None, token: str | None = None, timeout_s: float = 20.0
) -> list[dict]:
    """拉题目列表（平台 list 即断点：correct_flag_count/is_completed）。

    平台级失败（HTTP/网络/任务结束）统一抛 BenchmarkUnavailable（带可行动提示）；
    此前 HTTPError 直接裸抛 → solve 顶层 traceback 崩溃。
    """
    base = (base or os.environ.get("BENCHMARK_BASE_URL", "")).rstrip("/")
    token = token or os.environ.get("BENCHMARK_TOKEN", "")
    if not base or not token:
        raise BenchmarkUnavailable(
            "BENCHMARK_BASE_URL / BENCHMARK_TOKEN 未设置（solve 预检应已拦截）"
        )
    req = urllib.request.Request(
        f"{base}/openapi/v1/challenges",
        headers={"BENCHMARK_TOKEN": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise _platform_error(e) from None
    except urllib.error.URLError as e:
        raise BenchmarkUnavailable(
            f"平台连接失败: {getattr(e, 'reason', e)}（检查 VPN 是否连通，--vpn 可自举）"
        ) from None
    if not isinstance(data, list):
        raise BenchmarkUnavailable(f"题目列表格式异常: {str(data)[:200]}")
    return data


def _load_fail_digest(title: str) -> str:
    """重试失败上下文：按 title 指纹取回上一轮 settle 摘要（frontier 写台账）。

    让重试"带记忆换路线"，而不是从零重打（实测 19 道 exhausted 全是路线问题）。
    口径修正（1323 复盘）：engine "诚实收尾"也记为 done——若把 done 当成功跳过，
    失败题永远不会被注入摘要。是否重跑由平台进度（flags_done/段筛选）决定，
    这里只排除旧版 success 语义；取不到返回空串。
    """
    try:
        from secpt.core.orchestrator.frontier import settle_digest_key

        p = (
            Path(os.environ.get("SECPT_VAR", "var"))
            / "knowledge"
            / "settle_digests"
            / f"{settle_digest_key(title)}.json"
        )
        if not p.is_file():
            return ""
        d = json.loads(p.read_text(encoding="utf-8"))
        if str(d.get("status") or "") == "success":
            return ""
        tail = str(d.get("tail") or "")[:900]
        return (
            "\n\n【上一轮尝试记录（务必换路线，勿原样重跑）】"
            f"status={d.get('status')} steps={d.get('steps')} "
            f"{str(d.get('fail_reason') or '')[:120]}\n"
            f"上一轮最后的结论/卡点摘要：\n{tail}"
        )
    except Exception:
        return ""


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
每题有硬步数预算（按难度 40/60/110 × 旗数放大），烧完系统强制换题——
烧掉预算的 2/3 仍未出 flag 时你必须先做决策（hint 或主动收尾换题），不要等系统强停。
开局规范（编排层预置，勿浪费步数）：
- api.py 已在你的工作区预置好，直接 `python3 api.py ...` 使用，**不要重写**；
- probe.py 已预置（通用 HTTP 探测，输出绝对 URL 自动入资产图）——路径探测/
  端点快筛优先用它，**不要每个意图手写 http 客户端脚本**；
- 环境变量探测直接跑 `env`（白名单内），**不要加管道/重定向**（会被拦烧步）；
- start 返回 container_addr 后，用 `python3 api.py wait <ip:port>` 自适应等端口就绪
  （内部每 2s TCP 探测、最长 40s）——**不要盲等 `sleep 20`**（1323 实测整场 120 次
  盲等 = 40+ 分钟纯烧墙钟）；wait 返回 ready=false 时先 holders 看槽位再决策。
关闭纪律：绝不 close 其他题（含同进程兄弟 worker 的题）——只有编排层 settle 会关容器；
**也不 close 自己正在解的题再想重开**（close 后 3 槽被兄弟占满就永远 start 不回来，
1323 的 c-06 即死于此）：容器异常先重试原 addr/探测端口，确实要重启时把需求写进
ANSWER 交给编排层调度（它持有全局槽位视图）。
撞平台 409（配额/状态类）时：`api.py start` 已内置自动腾位重试（自动清理
结算残留/孤儿容器后再试一次），返回里带 auto_healed 字段即说明已自愈；
若仍 409 说明槽位全是在途题——先换题打，稍后回来重试，不要反复重试同一条命令。
保留域纪律：example/example.com/.invalid/.test 等保留域与带 SECPT- 标记的 URL 是平台
测试/金丝雀或占位地址，**不要主动请求**（含 DNS 探测/脚本内 urlopen）；外带类验证一律
只用平台下发的 container_addr 或题面明确指定的平台通道。
环境归因纪律：容器/平台访问超时或连接失败时，先跑 `python3 api.py health`：
  health.platform=false 且 ended=true → 平台任务已结束，立即收尾，不要再开新题；
  health.platform=false 且 started=false（网络错误）→ 环境异常（平台/VPN 不可达），
      保留现场，不要 close，ANSWER: 环境异常；
  health.vpn=false 但 health.platform=true → **忽略 VPN**（本环境不依赖 VPN），继续解题；
  health 平台可达但目标端口不通 → 才可判定靶场不可解，写进结论。
完成后输出 ANSWER: <unique_code 结果：拿到几个 flag / 得分 / 卡点一句话>。
"""

# P0-5：api.py 模板（编排层预置，worker 首步 write_file 直接落盘，不再探索/重写）
# ——跑分日志里每个 worker 都要花 2-3 步写 api.py，模板化省步数。
API_PY_TEMPLATE = '''#!/usr/bin/env python3
"""评测平台 API 客户端（编排层预置模板）。用法：
    python3 api.py list                         # 拉题目列表
    python3 api.py holders                      # 账本占用快照（谁正持有哪题/租约剩余）
    python3 api.py health                       # 三重健康探针（VPN/平台/是否已结束）
    python3 api.py start <unique_code>          # 启动容器拿 container_addr
    python3 api.py wait <ip:port>               # 自适应等端口就绪（≤40s，替代盲等 sleep）
    python3 api.py submit <unique_code> <flag>  # 提交 flag
    python3 api.py close <unique_code>          # 关闭容器（仅清理无有效租约的孤儿；在途题拒绝）
"""
import json
import os
import socket
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


def ledger_close_candidates():
    """可安全腾位的候选（api.py start 409 自愈用）：已完成生命周期或租约已过期的登记项。

    - solved/exhausted/failed：生命周期已结束，容器属结算滞后残留，关了不伤在途；
    - claimed/running 但租约已过期：worker 已死的孤儿容器。
    活租约（含同进程兄弟 worker）绝不出现在这里——那是别人的在途题。
    """
    try:
        import sqlite3

        path = os.environ.get(
            "SECPT_LEDGER_DB",
            os.path.join(os.environ.get("SECPT_VAR", "var"), "benchmark_ledger.sqlite"),
        )
        if not os.path.exists(path):
            return []
        conn = sqlite3.connect(path)
        now = time.time()
        rows = conn.execute(
            "SELECT unique_code, status FROM challenges"
            " WHERE status IN ('solved','exhausted','failed')"
            " OR (status IN ('claimed','running') AND lease_until <= ?)"
            " ORDER BY updated_at ASC LIMIT 2",
            (now,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        return []


def health():
    """A3 三重健康探针：VPN 网关 / 平台 API / 任务是否已结束。

    网络异常时先跑它，区分「环境故障」与「题不可解」——前者不应写进否定路线。
    """
    probe = os.environ.get("SECPT_VPN_PROBE_URL", "http://10.0.100.58").strip()
    # off/none/空 = 本环境无 VPN（比赛环境）：vpn 返回 null，不参与环境归因
    vpn = None
    if probe and probe.lower() not in ("off", "none", "0", "disable"):
        vpn = False
        try:
            with urllib.request.urlopen(probe, timeout=5) as r:
                vpn = b'"ok"' in r.read(500)
        except Exception:
            vpn = False
    platform = False
    ended = False
    try:
        data = call("GET", "/openapi/v1/challenges")
        if isinstance(data, list):
            platform = True
        elif isinstance(data, dict) and data.get("error") in (404, 409):
            ended = True
    except Exception:
        platform = False
    return {"vpn": vpn, "platform": platform, "ended": ended}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "list":
        out = call("GET", "/openapi/v1/challenges")
    elif cmd == "holders":
        # 状态同步读出通道：全系统容器持有情况（start 撞 409 / 决定 close 前先看这里）
        out = {"holders": ledger_holders()}
    elif cmd == "health":
        out = health()
    elif cmd == "start" and rest:
        out = call("POST", f"/openapi/v1/challenges/start?unique_code={rest[0]}")
        if isinstance(out, dict) and out.get("error") == 409:
            # 409 自愈：自动腾位重试（模型零步数）——只关"结算滞后残留/孤儿"，
            # 活租约（含兄弟 worker 的在途题）由 ledger_close_candidates 挡在外面
            healed = []
            for code, st in ledger_close_candidates():
                r = call("POST", f"/openapi/v1/challenges/close?unique_code={code}")
                if not (isinstance(r, dict) and r.get("error")):
                    healed.append(f"{code}({st})")
            if healed:
                time.sleep(1)
                out = call("POST", f"/openapi/v1/challenges/start?unique_code={rest[0]}")
                out["auto_healed"] = healed
        if isinstance(out, dict) and out.get("error") == 409:
            # 自愈后仍 409 = 槽位全是在途题：交还模型决策（附全局占用快照）
            out["holders"] = ledger_holders()
            out["hint"] = (
                "平台并发上限冲突且无孤儿可清理。claimed=已预留未起容器；"
                "running=容器在途（可能含本进程兄弟 worker）。在途题一律不可 close，"
                "等其 settle 自动回收；先打别的题，稍后回来重试 start。"
            )
    elif cmd == "wait" and rest:
        # 自适应就绪等待：start 返回 container_addr 后用它，端口一通立即返回——
        # 替代 "sleep 20 再探测" 的盲等（1323 实测 120 次 sleep 20 = 整场 40 分钟）。
        # 只做 TCP 连通性轮询，不打平台 API，不产生额外计费/限额。
        addr = rest[0]
        host, _, port_s = addr.rpartition(":")
        try:
            port = int(port_s)
            deadline = time.time() + float(os.environ.get("SECPT_WAIT_S", "40"))
        except ValueError:
            sys.exit(f"wait 需要 <ip:port>，收到 {addr!r}")
        started = time.time()
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=3):
                    ready = True
                    break
            except OSError:
                time.sleep(2)
        out = {
            "addr": addr,
            "ready": ready,
            "waited_s": round(time.time() - started, 1),
        }
        if not ready:
            out["hint"] = "端口 40s 未就绪：先 api.py holders 看槽位，避免盲目重 start"
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

# 通用 HTTP 探测模板（与 api.py 同思路：编排层预置省脚手架步）。
# 每行输出绝对 URL——引擎 finding 正则识别后由 GraphScribe 自动落资产图，
# 自写探测脚本（相对路径输出）是图记忆断粮的主因（1036 复盘）。
PROBE_PY_TEMPLATE = '''#!/usr/bin/env python3
"""通用 HTTP 探测（编排层预置，勿重写）。输出绝对 URL，图记忆自动摄取。

用法:
  python3 probe.py <url> [url2 ...]                # GET 多个 URL（并发 8 线程）
  python3 probe.py -X POST <url> '<json_body>'     # POST JSON
  python3 probe.py -H 'Cookie: a=b' -H 'X: 1' <url>...
"""
import http.client
import re
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


def request(method, url, body=None, extra=None, timeout=8):
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, port, timeout=timeout)  # TLS 校验默认开启（红线，不关）
    headers = {"User-Agent": "probe/1.0", "Accept": "*/*"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers.update(extra or {})
    conn.request(method, u.path or "/", body=body, headers=headers)
    r = conn.getresponse()
    data = r.read(8192)
    hd = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    return r.status, hd, data


def probe(url, method="GET", body=None, extra=None):
    try:
        status, hd, data = request(method, url, body=body, extra=extra)
        m = re.search(rb"<title[^>]*>(.*?)</title>", data, re.S | re.I)
        title = m.group(1).decode("utf-8", "replace").strip()[:60] if m else ""
        print(
            f"[{status}] {url} len={len(data)}"
            + (f" loc={hd.get('location', '')}" if hd.get("location") else "")
            + (f" title={title}" if title else ""),
            flush=True,
        )
    except Exception as e:
        print(f"[ERR] {url} {e}", flush=True)


def main():
    args = sys.argv[1:]
    method, body, extra, urls = "GET", None, {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-X" and i + 1 < len(args):
            method = args[i + 1]
            i += 2
        elif a == "-p" and i + 1 < len(args):
            body, method = args[i + 1], "POST"
            i += 2
        elif a == "-H" and i + 1 < len(args):
            k, _, v = args[i + 1].partition(":")
            extra[k.strip()] = v.strip()
            i += 2
        else:
            urls.append(a)
            i += 1
    if not urls:
        sys.exit(__doc__)
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda u: probe(u, method, body, extra), urls))


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
    max_parallel: int | None = None,  # 并发 worker 数；None → 读 env，缺省 3
    # （打满平台容器上限换吞吐；1323 的 c-06 死锁根因是"链 close 自己的容器想重开"，
    # 已由 worker 纪律条款治理而非降并发——时间盒里少 1 worker ≈ 少跑 12+ 题。
    # 需留槽跑：SECPT_BENCHMARK_WORKERS=2）
    llm=None,
    model: str | None = None,
    frontier: Frontier | None = None,
    ledger: ChallengeLedger | None = None,  # P1：跨进程互斥账本（防重复解题）
    deadline_ts: float | None = None,  # M1：本轮全局截止（epoch 秒；到点排空）
    reserve_s: float = 300.0,  # 截止前预留（收尾/关容器）
) -> dict:
    """并行跑分主入口：选题入队（账本 claim 防跨进程重复）→ frontier 多 worker 并行。"""
    if max_parallel is None:
        max_parallel = int(os.environ.get("SECPT_BENCHMARK_WORKERS", "3"))
    # P1：跨进程互斥账本（默认启用）——本进程已 claim 的题目才入队，
    # 其他 worker 已 claim / 已 solved 的题目跳过（省容器额度与步数）
    led = ledger or ChallengeLedger()

    # 双开闸门（复评修复）：同任务已有活跃 run（其他存活 worker 持未过期租约）→
    # 快速拒绝——两个 run × 2 worker 争 3 容器上限会互相饿死（实测 62 次 409、
    # 1.5 小时只 settle 2 次）。死进程残留租约由 active_foreign_leases 过滤；
    # 确需强制并行：SECPT_BENCHMARK_FORCE=1
    if os.environ.get("SECPT_BENCHMARK_FORCE") != "1":
        active = led.active_foreign_leases()
        if active:
            raise BenchmarkUnavailable(
                f"同一跑分任务已有活跃 run（{len(active)} 个未过期租约："
                f"{'、'.join(active[:5])}{'…' if len(active) > 5 else ''}）——"
                "并发跑分会争抢平台容器上限导致双方 409 饿死。请等待其结束后重跑；"
                "确认要强制并行请设 SECPT_BENCHMARK_FORCE=1。"
            )

    challenges = fetch_challenges()
    picked = _sorted_open(challenges)
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
        level = c.get("level")
        desc = str(c.get("description") or "")[:300]
        base_budget = DIFF_STEP_BUDGET.get(diff, PER_CHALLENGE_STEP_BUDGET)
        # A1：多旗题分段（每段 2 旗）——1323 的 b-01(2/4)/b-02(2/6) 是单意图串行，
        # 任何一旗卡住全线停摆；分段后逐段结算：拿一旗立即计分，某段失败不阻塞其余段。
        # 段间用 asset_scope 串行 + 优先级排序（不用硬依赖：前段失败时后段仍可独立尝试）。
        chunks = chunk_ranges(flags_total)
        n_seg = len(chunks)
        tools = tools_for_challenge(desc)  # C2：按题族动态启用插件工具
        playbook = _playbook_section(uc)[:600]  # B2：族级打法（自动蒸馏）
        # M1-B：意图级墙钟安全网（步数 × 每步墙钟估计，防单次网络挂死吃掉整轮）；
        # 0=关闭。默认 10s/步（1323 实测均值 7s/步 + 余量）
        _wall_mult = float(os.environ.get("SECPT_BENCH_STEP_WALL_S", "10") or 0)
        for ci, (start, end) in enumerate(chunks, 1):
            if end <= flags_done:
                continue  # A2：本段旗数已在平台到账——重试轮不重复跑（省整段预算）
            chunk_size = end - start + 1
            mult = min(4, max(1, (chunk_size + 1) // 2))
            budget = base_budget * mult
            seg = f"·段{ci}/{n_seg}" if n_seg > 1 else ""
            # title 保持跨轮稳定（段号固定）——重试摘要台账按 title 指纹取回
            title = f"通关 {uc}（{c.get('difficulty')} L{level}）{seg}"
            goal = (
                f"unique_code={uc}；description={desc}；\n"
                f"本题共 {flags_total} 旗，平台当前已得 {flags_done} 旗。"
                f"本段目标：第 {start}~{end} 旗中至少拿到 1 个新 flag 并立即 submit"
                f"（每拿一旗就交一旗，不要攒）。\n"
                f"先用 api.py start {uc} 拿 container_addr（已开则直接复用 list 里的地址），"
                f"解题后 submit，最后务必 close。脚本命名带 {uc} 前缀。"
                f"{playbook}"
                f"{_load_fail_digest(title)}"
            )
            fr.add(
                title,
                goal=goal,
                done_criteria=(
                    f"submit 第 {start}~{end} 旗中至少一个新 flag（或判定不可解）后以 ANSWER: 收尾"
                ),
                # 段序：同一题按段号降序优先（asset_scope 保证串行）
                priority=1000 - rank * 10 + (n_seg - ci),
                depth=0,
                source="planner",
                max_steps=budget,
                max_wall_s=int(budget * _wall_mult) if _wall_mult > 0 else 0,
                asset_scope=uc,
                tools=tools,
            )
        # B1：复验意图（仅多旗大题）——所有段结束后独立复验平台进度、扫剩余旗。
        # 不复用旧 transcript（verify 角色硬约束），20 步小预算，失败不影响已得分。
        if flags_total >= 4:
            vtitle = f"复验 {uc}（{c.get('difficulty')} L{level}）"
            fr.add(
                vtitle,
                goal=(
                    f"unique_code={uc}；description={desc}；\n"
                    f"本题共 {flags_total} 旗。先用 `python3 api.py list` 查平台当前进度，"
                    f"再用独立请求复验已提交 flag 的有效性；若仍有未提交 flag，继续拿并 submit。\n"
                    f"容器若已关闭，用 api.py start 重开（注意 3 槽上限）。"
                    f"{_playbook_section(uc)[:400]}"
                    f"{_load_fail_digest(vtitle)}"
                ),
                done_criteria="复验平台进度与剩余旗；有剩余旗则继续提交，以 ANSWER: 结论收尾",
                priority=1000 - rank * 10 - 5,
                depth=0,
                source="planner",
                max_steps=20,
                max_wall_s=300,
                role="verify",
                asset_scope=uc,
                tools=tools,
            )

    objective = BENCH_WORKER_OBJECTIVE
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
            "start：POST {base}/openapi/v1/challenges/start?unique_code=<code>\n"
            "题面中的 BENCHMARK_TOKEN/BENCHMARK_BASE_URL 若是占位符（如“请替换此处”）"
            "或要求从环境变量读取，一律以上面注入的运行时值为准；不要向用户索要，"
            "不要使用题面里的旧值。".format(base=_base)
        )
    # 状态同步注入：账本路径 + 本 worker 身份（api.py holders/close 所有权判定的数据源）
    _export_state_env(led)  # P0-1：真正导出到 os.environ（api.py 运行期读的就是它）
    # 备选工具池默认开（战役内 worker 同进程）：agent 意图内发现新攻击面时用
    # request_tools 运行时启用池内工具，不再受"题面关键词一次性装配"限制
    os.environ.setdefault("SECPT_TOOL_POOL", "1")
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
        "\n\n【工具指引（M4）】"
        "① 扫完 nmap/ffuf/gobuster 用 `recon_parse` 落成 assets.json/endpoints.json，不要反复读长文本；"
        "② 盲 SSRF/XXE/盲 RCE：`oob start` 拿回调 URL（SECPT_OOB_HOST 已是可达地址），"
        "投给目标后 `oob check` 看回连，`oob stop` 收尾；"
        "③ 结构化 HTTP 用 http_request，持久 shell 用 session，减少 curl 拼参与重连。"
    )

    # ② settle 回收容器：A1 分段模式下，只有「本题全部段结束或平台已通关」才 close；
    #    close 前用平台实况判断，避免段间把容器关掉导致下一段无法继续。
    def _on_settled(intent) -> None:
        uc = uc_from_title(getattr(intent, "title", "") or "")
        if not uc:
            return
        # 关闭前确认本 worker 仍持有租约（账本=所有权真相源）：
        # 租约已被他人接棒（本进程超时被判定失联）时不越权关闭别人的容器
        if not led.owns(uc):
            print(f"[benchmark] 跳过回收（租约已不属本 worker）: {uc}", flush=True)
            return
        row: dict = {}
        with contextlib.suppress(BenchmarkUnavailable):
            row = _platform_snapshot().get(uc, {})
        completed = bool(
            row
            and (
                row.get("is_completed")
                or int(row.get("correct_flag_count") or 0) >= int(row.get("flag_count") or 1)
            )
        )
        if completed:
            # 平台已全通：剩余段直接落 done，省掉无意义步数（分段模式的核心收益）
            _n = fr.finish_open_by_asset(uc, result="平台已通关，跳过剩余段")
            if _n:
                print(f"[benchmark] {uc} 平台已通关，跳过剩余 {_n} 段", flush=True)
            with contextlib.suppress(Exception):
                led.mark_solved(uc, flags_found=int(row.get("flag_count") or 1))
        remaining = fr.count_active_by_asset(uc)
        if not completed and remaining > 0:
            return  # 还有后续段要跑，容器保持
        if _close_challenge(uc):
            print(f"[benchmark] settle 回收容器: {uc}", flush=True)

    # P0-2：状态语义分离——claim=已预留、running=容器在途。意图「真正开始执行」时才置
    #    running（frontier 的 on_started 回调），而非启动即把全部题目标 running。
    #    frontier 多 worker 受 max_parallel 约束，账本 running 数 ≈ 真实在途容器数。
    def _on_started(intent) -> None:
        uc = uc_from_title(getattr(intent, "title", "") or "")
        if not uc:
            return
        # P2：api.py 预落盘 worker 工作区（frontier worker job_id 约定 <base>-i<id后4位>）。
        # tsec1-run-1213 复盘：每个 worker 开局花 1-2 步重写 api.py（还写出不同版本），
        # 编排层已经把模板注入 objective，直接确定性落盘，模型"用而不写"。
        try:
            from secpt.core.job.resume import jobs_dir

            ws = jobs_dir() / f"{spec.job_id}-i{intent.id[-4:]}" / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            api = ws / "api.py"
            if not api.exists():  # 模型自定义过则不覆盖（resume 场景尊重现场）
                api.write_text(API_PY_TEMPLATE, encoding="utf-8")
            probe = ws / "probe.py"
            if not probe.exists():  # 通用 HTTP 探测（输出绝对 URL 喂资产图）
                probe.write_text(PROBE_PY_TEMPLATE, encoding="utf-8")
        except Exception as e:
            print(f"[benchmark] api.py 预落盘失败（忽略，模型自写）: {e}", flush=True)
        if uc:
            with contextlib.suppress(Exception):
                led.mark_running(uc)

    # ① 租约心跳 + A3 平台期限探针：每 60s 续租，并检查任务是否已被平台结束；
    #    结束时置 drain 事件 → frontier 停止认领新意图（在途链自然收尾，不再开新题）
    stop_hb = threading.Event()
    drain_ev = threading.Event()

    def _hb_loop() -> None:
        while not stop_hb.wait(60.0):
            for c, _ft in claimed:
                with contextlib.suppress(Exception):
                    led.heartbeat(str(c.get("unique_code")))
            try:
                _platform_snapshot(force=True, ttl=0)
            except BenchmarkEnded:
                if not drain_ev.is_set():
                    print("[benchmark] 平台任务已结束 → 优雅排空（停止开新题）", flush=True)
                    drain_ev.set()
            except BenchmarkUnavailable:
                pass  # 网络抖动：下个周期再探，不中断在途链

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
            # 清单型任务关 planner（复评修复）：预置题即全部工作，规划层只在
            # "探索型"入口（hunt）才有意义——此前未传 max_intents 撞缺省 60，
            # 题数<60 时队列排空后 planner 会给已完成题产"幽灵意图"（容器全关
            # 任务还在跑的直接原因）；钉死 total==max → can_plan 恒 False
            max_intents=len(claimed),
            max_rounds=len(picked) + 5,
            stop_on_first_flag=False,  # 跑分模式：解完所有已入队题目，不因单个 flag 收敛
            on_started=_on_started,  # P0-2：意图真正开跑时才置 running
            on_settled=_on_settled,  # ② 结算收尾：确定性 close 本题容器
            scope_ledger=led,  # ③ 广播载体（benchmark 意图无 asset_scope，仅统一实例）
            should_drain=drain_ev.is_set,  # A3：平台结束后停止认领新意图
            deadline_ts=deadline_ts,  # M1：本轮全局截止（campaign 可传）
            reserve_s=reserve_s,
        )
    finally:
        stop_hb.set()  # frontier 结束即停心跳（结算阶段不再续租）
    summary["mode"] = "benchmark(parallel)"
    summary["challenges_total"] = len(challenges)
    summary["challenges_picked"] = len(picked)

    # A3：轮末容器清扫——本题未通关的容器必须关掉，否则下一轮 start 撞 3 槽上限。
    # （分段模式中途不关；轮末所有链已 settle，安全。平台已结束时跳过——端点全 409，
    #  逐个 close 只会把失败超时串起来空转）
    if not drain_ev.is_set():
        for c, _ft in claimed:
            uc = str(c.get("unique_code"))
            if not led.is_completed(uc):
                _close_challenge(uc)

    # P1 账本结算（tsec1-run-1213 假 solved 修复）：done 只代表"链收尾"，
    # 不代表拿分——必须重新拉平台实况核实，凭 correct_flag_count 落账：
    #   flags_found >= flags_total（或平台 is_completed）→ solved
    #   0 < flags_found < flags_total              → exhausted（部分旗，可重试续打）
    #   fetch 失败                               → 全部不标 solved（fail-closed，
    #     宁可下轮重打也不把没分的题标 solved 永久跳过）
    # 注：不看 chains 的 done 集合——平台实况比链自报更准（链可能"诚实收尾"零分）。
    live: dict[str, dict] = {}
    verify_ok = True
    with contextlib.suppress(Exception):
        try:
            live = {str(c.get("unique_code")): c for c in fetch_challenges()}
        except Exception as e:
            verify_ok = False
            print(f"[benchmark] 结算核实失败（不标 solved，全部记可重试）: {e}", flush=True)
    for c, flags_total in claimed:
        uc = str(c.get("unique_code"))
        st = live.get(uc) or {}
        flags_found = int(st.get("correct_flag_count") or 0)
        completed = bool(st.get("is_completed"))
        if not verify_ok:
            led.mark_failed(uc, reason="环境异常（结算时平台不可达，不计路线失败；下轮重试）")
        elif flags_found >= flags_total or (completed and flags_found > 0):
            led.mark_solved(uc, flags_found=flags_found, flags_total=flags_total)
        else:
            led.mark_failed(uc, reason="本轮未完成（步数耗尽/换题）")
            if flags_found > 0:  # 部分旗进度回填，下轮从缺口续打
                led.set_flags_found(uc, flags_found)
    # M0：本轮按题/族指标（triage 与复盘的唯一数据源；不参与结算判定）
    summary["challenges_status"] = {
        str(c.get("unique_code")): {
            "difficulty": c.get("difficulty"),
            "flags_found": int(
                (live.get(str(c.get("unique_code"))) or {}).get("correct_flag_count") or 0
            ),
            "flags_total": flags_total,
            "completed": bool((live.get(str(c.get("unique_code"))) or {}).get("is_completed")),
        }
        for c, flags_total in claimed
    }
    summary["families"] = _family_metrics(summary["challenges_status"])
    summary["ledger"] = led.summary()
    with_json = Path(os.environ.get("SECPT_VAR", "var")) / "benchmark_ledger.json"
    led.dump_json(with_json)
    return summary


def _family_metrics(challenges_status: dict[str, dict]) -> dict[str, dict]:
    """按题族聚合本轮进度：{fam: {n, flags, max_flags, completed}}。"""
    fams: dict[str, dict] = {}
    for uc, s in challenges_status.items():
        fam = re.match(r"[a-z]+\d*", uc.lower())
        key = fam.group(0) if fam else "?"
        row = fams.setdefault(key, {"n": 0, "flags": 0, "max_flags": 0, "completed": 0})
        row["n"] += 1
        row["flags"] += int(s.get("flags_found") or 0)
        row["max_flags"] += int(s.get("flags_total") or 0)
        row["completed"] += 1 if s.get("completed") else 0
    return fams


# ───────────────────────── A2：战役（campaign）多轮编排 ─────────────────────────


def campaign_state_path(spec: TaskSpec) -> Path:
    """战役状态文件：var/campaigns/<task_id>.json（跨轮持久，C1）。"""
    task = os.environ.get("BENCHMARK_TASK_ID", "") or spec.job_id
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", task)[:80]
    return Path(os.environ.get("SECPT_VAR", "var")) / "campaigns" / f"{safe}.json"


def _load_campaign_state(path: Path) -> dict:
    with contextlib.suppress(Exception):
        return json.loads(path.read_text(encoding="utf-8"))
    return {"rounds": [], "created_at": time.time()}


def _save_campaign_state(path: Path, state: dict) -> None:
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = time.time()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


def _campaign_round_model(model: str | None, n: int) -> str | None:
    """轮间换脑（PA）：第 2+ 轮未完成题用升级模型重打——换路线之外再换"脑子"。

    第 1 轮已带 digest 记忆换路线，但同源模型盲区相同；第 2 轮起切
    SECPT_ESCALATE_MODEL（跨厂商第二脑）给出异源视角。SECPT_CAMPAIGN_ROTATE_MODEL=0 关。
    """
    if n > 1 and os.environ.get("SECPT_CAMPAIGN_ROTATE_MODEL", "1") != "0":
        alt = os.environ.get("SECPT_ESCALATE_MODEL", "").strip()
        if alt and alt != model:
            return alt
    return model


async def run_benchmark_campaign(
    spec: TaskSpec,
    *,
    rounds: int | None = None,
    llm=None,
    model: str | None = None,
) -> dict:
    """A2：自动多轮跑分——每轮结束以平台实况为准，未完成题释放租约后重试。

    1323 的教训：digest 里写满了第二轮该打哪，但一轮 4.7h 后任务到时限，
    17 道失败题一次重试都没有。轮次预算（SECPT_CAMPAIGN_ROUNDS，默认 3）与
    总墙钟（SECPT_CAMPAIGN_WALL_S，默认 0=不限）由 env 控制；平台任务结束时
    （BenchmarkEnded）立即停止，不再空转。
    """
    if rounds is None:
        rounds = int(os.environ.get("SECPT_CAMPAIGN_ROUNDS", "3"))
    rounds = max(1, rounds)
    wall_s = float(os.environ.get("SECPT_CAMPAIGN_WALL_S", "0") or 0)
    t0 = time.time()
    sp = campaign_state_path(spec)
    state = _load_campaign_state(sp)
    state.setdefault("rounds", [])
    state["task"] = os.environ.get("BENCHMARK_TASK_ID", "") or spec.job_id
    last: dict | None = None

    # B2/C3：开跑前用历史 digest 蒸馏族级打法（best-effort，失败不阻断）
    with contextlib.suppress(Exception):
        from secpt.learning.playbook import build_playbooks, distill_experiences

        build_playbooks()
        distill_experiences()

    # P5（1036 复盘）：中断优雅终态——默认 KeyboardInterrupt 直接穿透，不做任何
    # 租约回收，幽灵租约最长挡别人 900s。先清理租约再抛 KeyboardInterrupt，
    # 保持中断语义不变；非主线程注册失败则静默降级（TTL 兜底）。
    def _on_signal(signum, _frame):
        with contextlib.suppress(Exception):
            ChallengeLedger().release_own_claims(reason=f"campaign sig{signum} 中断")
        print(
            f"[campaign] 收到信号 {signum}：本进程租约已释放，退出"
            f"（{len(state['rounds'])} 轮记录）",
            flush=True,
        )
        raise KeyboardInterrupt

    with contextlib.suppress(Exception):
        for _sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(_sig, _on_signal)

    for n in range(1, rounds + 1):
        if n > 1:
            # 轮间：释放上一轮本进程租约（claimed/running → exhausted 可重领，solved 不动）
            released = 0
            with contextlib.suppress(Exception):
                released = ChallengeLedger().release_own_claims(
                    reason=f"campaign 第 {n - 1} 轮结束"
                )
            try:
                live = _platform_snapshot(force=True, ttl=0)
            except BenchmarkEnded as e:
                print(
                    f"[campaign] 平台任务已结束，停止后续轮次: {str(e).splitlines()[0]}", flush=True
                )
                break
            except BenchmarkUnavailable as e:
                print(f"[campaign] 平台不可达，停止后续轮次: {str(e).splitlines()[0]}", flush=True)
                break
            pending = [
                uc
                for uc, c in live.items()
                if not (
                    c.get("is_completed")
                    or int(c.get("correct_flag_count") or 0) >= int(c.get("flag_count") or 1)
                )
            ]
            if not pending:
                print("[campaign] 全部题目已通关，无需下一轮", flush=True)
                break
            if wall_s > 0 and time.time() - t0 > wall_s:
                print(f"[campaign] 战役墙钟 {wall_s:.0f}s 已用尽，停止", flush=True)
                break
            print(
                f"[campaign] 第 {n} 轮：{len(pending)} 题未完成，重试"
                f"（释放 {released} 个上一轮租约）",
                flush=True,
            )
        print(f"[campaign] ===== 第 {n}/{rounds} 轮开始 =====", flush=True)
        _round_t0 = time.time()
        _deadline = (t0 + wall_s) if wall_s > 0 else None
        _round_model = _campaign_round_model(model, n)
        if _round_model != model:
            print(
                f"[campaign] 第 {n} 轮换脑：{_round_model.split('/')[-1]}"
                f"（第 1 轮: {(model or 'primary').split('/')[-1]}）",
                flush=True,
            )
        summary = await run_benchmark(spec, llm=llm, model=_round_model, deadline_ts=_deadline)
        last = summary
        _fam = summary.get("families") or {}
        _flags_found = sum(
            int(s.get("flags_found") or 0)
            for s in (summary.get("challenges_status") or {}).values()
        )
        _flags_total = sum(
            int(s.get("flags_total") or 0)
            for s in (summary.get("challenges_status") or {}).values()
        )
        state["rounds"].append(
            {
                "n": n,
                "ts": time.time(),
                "elapsed_s": round(time.time() - _round_t0, 1),
                "picked": summary.get("challenges_picked"),
                "flags": len(summary.get("flags") or []),
                "flags_found": _flags_found,
                "flags_total": _flags_total,
                "families": _fam,
                "ledger": summary.get("ledger", {}),
                "challenges_status": summary.get("challenges_status", {}),
            }
        )
        _save_campaign_state(sp, state)
        _fam_txt = ", ".join(
            f"{k}:{v.get('flags')}/{v.get('max_flags')}" for k, v in sorted(_fam.items())
        )
        print(
            f"[campaign] 轮 {n} 指标: picked={summary.get('challenges_picked')} "
            f"flags={_flags_found}/{_flags_total} wall={time.time() - _round_t0:.0f}s "
            f"families={{{_fam_txt}}}",
            flush=True,
        )
        # 轮后重蒸馏：本轮新 digest 立刻进入下一轮 playbook（同族经验滚动累积）
        with contextlib.suppress(Exception):
            from secpt.learning.playbook import build_playbooks, distill_experiences

            build_playbooks()
            distill_experiences()
        # 轮后判定：平台实况决定是否继续
        try:
            live = _platform_snapshot(force=True, ttl=0)
        except BenchmarkEnded:
            break
        except BenchmarkUnavailable:
            break
        pending = [
            uc
            for uc, c in live.items()
            if not (
                c.get("is_completed")
                or int(c.get("correct_flag_count") or 0) >= int(c.get("flag_count") or 1)
            )
        ]
        if not pending:
            print("[campaign] 全部题目已通关", flush=True)
            break
        if wall_s > 0 and time.time() - t0 > wall_s:
            print(f"[campaign] 战役墙钟 {wall_s:.0f}s 已用尽，停止", flush=True)
            break

    if last is None:  # 一轮都没跑（理论上不出现）：退回单轮
        last = await run_benchmark(spec, llm=llm, model=model)
    last["campaign"] = {
        "rounds_done": len(state.get("rounds") or []),
        "state_path": str(sp),
        "elapsed_s": round(time.time() - t0, 1),
    }
    return last


def reconcile_ledger(led: ChallengeLedger | None = None, *, release_claims: bool = False) -> dict:
    """账本对账（假 solved 清理 + 部分旗进度同步）：以平台实况为唯一真相源。

    tsec1-run-1213 的教训：引擎把"诚实收尾但没拿分"的 ANSWER 也结算成 done，
    账本据此标 solved → 该题被永久跳过、分值丢失。本函数：
    - 本地 solved 但平台 correct_flag_count==0 且 is_completed=false → 降级 exhausted
      （可重试），并记录 last_error 说明降级原因；
    - 本地非 solved 但平台有分（correct_flag_count>0）→ 按平台进度回填 flags_found，
      满旗则标 solved；
    - 平台查不到的题号（旧任务代际残留）→ 原样保留，只在报告里列出；
    - release_claims=True（运维，重启场景）→ 额外强制释放全部 claimed/running 租约。
    返回对账报告 dict（CLI 打印 + 测试断言用）。
    """
    led = led or ChallengeLedger()
    live = {str(c.get("unique_code")): c for c in fetch_challenges()}
    report = {
        "downgraded": [],
        "synced": [],
        "solved_now": [],
        "unknown": [],
        "ok": 0,
        "released": 0,
    }
    for e in led.iter_entries():
        uc = str(e.get("unique_code"))
        st = live.get(uc)
        if st is None:
            if e.get("status") not in ("solved",):
                report["unknown"].append(uc)
            continue
        found = int(st.get("correct_flag_count") or 0)
        total = int(st.get("flag_count") or e.get("flags_total") or 1)
        completed = bool(st.get("is_completed"))
        if e.get("status") == "solved":
            if found > 0 or completed:
                report["ok"] += 1
            else:  # 假 solved：链收尾但没拿分 → 降级可重试
                led.mark_failed(uc, reason="对账降级：账本 solved 但平台无得分记录")
                report["downgraded"].append(uc)
        elif found >= total or (completed and found > 0):
            led.mark_solved(uc, flags_found=found, flags_total=total)
            report["solved_now"].append(uc)
        elif found > 0:
            led.set_flags_found(uc, found)
            report["synced"].append(uc)
    if release_claims:
        report["released"] = led.release_all_claims(reason="对账释放：跑分重启")
    led.dump_json(Path(os.environ.get("SECPT_VAR", "var")) / "benchmark_ledger.json")
    return report
