"""可复用渗透 Engine：把 harness.py 的主循环抽成参数化类。

主循环四条语义与 runtime/harness.py 完全一致：
a) messages 整条 append，从不 rebuild、从不按拍重建、从不切换 system prompt
b) assistant 消息原样 append（含 content 与 tool_calls），不做任何裁剪
c) 每个 tool_call 的结果 append 回同一条 messages
d) 模型以 ANSWER: <结论> 自宣告终止（flag 样式不固定，不做正则判定）

不含任何硬编码任务信息；先验/工具说明/约束由 RunConfig.system_extra 传入，
scope 约束由 governance 层负责。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import os
import re
import shlex
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import structlog

from secpt.runtime.keyguard import redact_private_keys
from secpt.runtime.logsetup import configure_logging
from secpt.runtime.metrics import counter_inc
from secpt.runtime.stuck import StuckDetector


# ---- 三档止损状态机（设计 docs/设计-工作循环形态.md §2）----
# 档位由状态机定，模型只负责内容质量（Cognition 教训：不要求模型自己知道该停）。
# 全部 env 可调；SECPT_LOOP_STOP=0 一键回退旧行为（只留 nudge + 硬预算墙）。
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _loop_stop_enabled() -> bool:
    return os.environ.get("SECPT_LOOP_STOP", "1") != "0"


def _wrap_after_nudge() -> int:
    return _env_int("SECPT_WRAP_AFTER_NUDGE", 8)


def _wrap_at_pct() -> float:
    return _env_float("SECPT_WRAP_AT_PCT", 95.0)


PROGRESS_ANCHOR_MARK = "【压缩锚点：结构化进度】"


def _progress_path(cfg: RunConfig):
    from pathlib import Path

    # 隐藏文件：progress.md 若以普通文件出现在 ws，会污染模型的 `ls` 观测
    # （内容每 5 步变化 → 卡死检测误判"出现新信息"，1323 修复中实测会打乱 nudge 节奏）
    return Path(cfg.work_dir) / ".progress.md"


def _redact_secrets(text: str) -> str:
    """进度锚点脱敏：环境里的真实凭证值与常见 token 键值一律打码。"""
    import re

    out = text
    for key in ("BENCHMARK_TOKEN", "DEEPSEEK_API_KEY", "JEV_API_KEY"):
        val = os.environ.get(key, "")
        if len(val) >= 8:
            out = out.replace(val, "***")
    out = re.sub(
        r"(?i)((?:BENCHMARK_TOKEN|API_KEY|TOKEN|SECRET)[\"']?\s*[:=]\s*[\"']?)"
        r"([A-Za-z0-9._\-]{8,})",
        r"\1***",
        out,
    )
    return out


def write_progress_anchor(
    cfg: RunConfig,
    *,
    steps: int,
    session_round: int,
    actions: list[tuple[int, str, str, str]],
    findings: int,
) -> None:
    """C1：把结构化进度落盘 ws/progress.md（确定性，不调 LLM）。

    长任务里压缩会删除旧消息；progress.md 是"外部记忆"——压缩/rollover 后把
    它的摘要作为锚点消息重新注入，模型不会丢失"做到哪、试过什么、什么被否定"。
    """
    obj = " ".join((cfg.objective or "").split())[:300]
    lines = [
        f"# 进度锚点（自动生成 · step {steps} · round {session_round}）",
        f"- 目标: {obj}",
        f"- 已记录发现: {findings}",
        "- 最近工具动作（旧→新）:",
    ]
    for st, name, args, res in actions:
        lines.append(f"  - step {st} `{name}` {args} → {res}")
    try:
        p = _progress_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 凭证脱敏：objective/工具参数可能含平台 token（消息里本来就有，但落盘
        # 文件不得留明文——沿用"绝不打印/落盘凭证"红线）
        p.write_text(_redact_secrets("\n".join(lines) + "\n"), encoding="utf-8")
    except Exception:
        pass  # 观测层 best-effort，绝不阻断主链路


def progress_anchor_text(cfg: RunConfig, limit: int = 2500) -> str:
    """读取 progress.md 生成锚点消息（无内容返回空串）。"""
    try:
        text = _progress_path(cfg).read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not text:
        return ""
    return (
        f"{PROGRESS_ANCHOR_MARK}\n{text[:limit]}\n"
        "（更早的消息已被压缩；以上是确定性进度记录。继续推进未完成目标，"
        "不要重复已否定的路线。）"
    )


def _wrap_min_steps(cfg: RunConfig) -> int:
    """nudge→wrap 升级的最低步数地板（1323 实证：a-05 第 15 步被"卡死检测→nudge
    →8 步无改善→wrap"级联截断，fuzz 期重复命令是正常行为，两条高概率路线未测）。
    低于地板不得进入收尾——宁可在 nudged 档继续跑（预算墙是最终兜底），也不让
    早期误伤把送分题掐死在探索期。有 max_steps 时取 35%，否则绝对地板 20。"""
    env = os.environ.get("SECPT_WRAP_MIN_STEPS", "")
    if env.strip():
        return _env_int("SECPT_WRAP_MIN_STEPS", 20)
    if cfg.max_steps > 0:
        return max(20, int(cfg.max_steps * 0.35))
    return 20


def _stall_after_wrap() -> int:
    return _env_int("SECPT_STALL_AFTER_WRAP", 8)


def _margin_window() -> int:
    return _env_int("SECPT_MARGIN_WINDOW", 20)


def _margin_cost_cny() -> float:
    return _env_float("SECPT_MARGIN_COST_CNY", 0.05)


WRAP_MSG = (
    "【收尾阶段】本步起进入收尾：剩余预算有限/本策略收益已趋零。\n"
    "只做三件事（严禁开新战线）：\n"
    "1. 用你已确认的事实给出当前结论（含证据：文件:行 / step N / 响应片段）\n"
    "2. 列出未完成项与已验证否定的路线（防后续会话重复踩坑）\n"
    "3. 以 ANSWER: <结论> 收尾（有 flag 给 flag；没有就给当前结论摘要）"
)

# ── PA4 终止与上限精细化 ──
# tool-call arguments JSON 解析失败的哨兵：不执行工具，回填自救错误串（Reflector 思想）。
# 用特殊 dict 而非 object()——下游 _call_key/_extract_refs 等对 args 调 .get/序列化，
# 哨兵必须能安全通过；身份比较（is）靠同一对象保证。
_UNPARSEABLE: dict = {"__secpt_unparseable__": True}

_TC_PARSE_RESCUE_MSG = (
    "【自救提示】已连续 {n} 次工具调用参数 JSON 解析失败（调用未执行）。修复方案：\n"
    "1. arguments 必须是合法 JSON 字符串——注意引号转义与换行符（\\n）\n"
    "2. 参数复杂/含大段文本时：改用 write_file 把内容写成文件，再用 bash 脚本读取处理\n"
    "3. 该模式常见于单次输出过长被截断——把一次要做的事拆小"
)


def _emit_loop_phase(
    engine_hooks: object, audit: object, log: object, *, phase: str, reason: str = ""
) -> None:
    """loop_phase 事件三通道：事件流（EventSink.write）> 审计 > 结构化日志。

    观测层 best-effort：任何通道失败都不阻断主循环（红线 3）。
    """
    try:
        if engine_hooks is not None and hasattr(engine_hooks, "write"):
            engine_hooks.write("loop_phase", {"phase": phase, "reason": reason})
            return
    except Exception:
        pass
    try:
        if audit is not None:
            audit.log("loop_phase", phase=phase, reason=reason)
            return
    except Exception:
        pass
    log.info("engine.loop_phase", phase=phase, reason=reason)


# 结构化日志：生命周期事件（start/progress/compact/rollover/nudge/fallback/fatal）
# 统一走 structlog（SECPT_LOG_JSON=1 → JSON，按 job_id 检索链路；默认控制台友好）
_log = structlog.get_logger("secpt.engine")

DEFAULT_BASH_WHITELIST = frozenset(
    {
        "curl",
        "python3",
        "base64",
        "xxd",
        "grep",
        "sed",
        "awk",
        "cat",
        "ls",
        "file",
        "openssl",
        "nmap",
        "whatweb",
        "find",
        "echo",
        "pwd",
        # 通用分析/工程命令（只读或工作目录内，跨靶场可迁移）
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
        "strings",
        "diff",
        "sha256sum",
        "unzip",
        "zipinfo",
        "mkdir",
        # 二进制/取证分析（只读、跨靶场通用）：反汇编/符号表/字节查看
        "objdump",
        "readelf",
        "nm",
        "od",
        "hexdump",
        # 通用分析/取证/逆向（跨靶场）：数据库客户端、元数据、文件雕刻、隐写、动态调试
        "sqlite3",
        "exiftool",
        "binwalk",
        "steghide",
        "gdb",
        "radare2",
    }
)

DEFAULT_FLAG_REGEX = r"[Ff][Ll][Aa][Gg]\{[^{}]{3,}\}"

# 模型上下文窗口表（cfg.context_window 未显式指定时按主模型查表）
MODEL_WINDOWS = {
    "deepseek-flash": 128_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
}

BASH_MAX_OUT = 8000
BASH_READ_CAP = BASH_MAX_OUT * 2  # 单命令 stdout+stderr 读取上限（防失控输出撑爆内存）
READ_MAX = 200 * 1024  # 兼容旧注：单次 read_file 默认读取上限（分页前的整文件上限）
READ_SLICE_MAX = BASH_MAX_OUT * 2  # read_file 单次分页上限（字符）
READ_HARD_CAP = 8 * 1024 * 1024  # read_file 可读文件上限（超出引导 bash 分段；防大文件读爆内存）

# shell 组合构造：白名单命令默认以 argv 数组执行（禁 shell=True），仅拦截
# 未引号的"跨命令依赖"构造（管道/重定向/串联/后台/命令替换）——这些必须靠
# shell 解释才能达成模型意图，拦截并引导脚本化。只有 profile 显式放行
# bash/sh 时才允许 shell 解释（此时以 bash -c 执行）。
_SHELL_EXES = ("bash", "sh")

# 跨命令依赖字符（未引用即拦）：| 管道 / ; 串联 / & 后台与 && || / > < 重定向。
# 裸 $VAR、通配符 *、反斜杠 \ 在 argv 直传下只是字面量（不经 shell 解释，
# 无注入面），P1 起放行按字面传参——需要展开/组合时引导写脚本（低摩擦）。
_SHELL_CHAIN_CHARS = "|;&<>"


def _has_unquoted_shell_chain(command: str) -> bool:
    """shell 组合构造仅统计引号外的：引号内的 ; | $ * 等是参数内容，argv 直传执行安全
    （如 python3 -c 'a;b'、sed 's|x|y|'、find -name "*.py"）。

    引号外命中以下构造说明模型期望 shell 解释器 → 拦截并引导脚本化：
    - 管道 |、重定向 >/<、分号串联 ;、后台/逻辑与或 &（&& / ||）
    - 命令替换 $(...) 与反引号 `...`
    裸 $VAR（变量字面量）、通配符 *（glob 字面量）放行（argv 直传按字面）。
    """
    quote: str | None = None
    for i, ch in enumerate(command):
        if quote:
            if ch == quote and command[i - 1] != "\\":
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in _SHELL_CHAIN_CHARS:
            return True
        elif ch == "$" and i + 1 < len(command) and command[i + 1] == "(":
            return True  # $(...) 命令替换
        elif ch == "`":
            return True  # 反引号命令替换
    return False


# ---------------------------------------------------------------------------
# 工具调用并行化（P0）：并行执行、串行记账。
# 依赖启发式只识别"写→读/跑"边，其余独立调用同组并发；依赖误判只会多串行
# （保守、安全），漏判（如脚本 import 兄弟脚本）属启发式的已知上限。
# ---------------------------------------------------------------------------

# 同轮独立工具调用并发上限默认值（E：SECPT_PARALLEL_MAX 可覆盖，clamp 1-16；
# 防单轮输出 token 截断 / 沙箱过载）
_PARALLEL_MAX = 4


def _parallel_max() -> int:
    """工具并发上限：SECPT_PARALLEL_MAX 覆盖（非法值回退默认，clamp 1-16）。"""
    try:
        v = int(os.environ.get("SECPT_PARALLEL_MAX", "") or _PARALLEL_MAX)
    except ValueError:
        v = _PARALLEL_MAX
    return max(1, min(16, v))


# bash 命令里形如文件名的 token（用于识别"写脚本→跑脚本"依赖）
_EXT_RE = re.compile(
    r"\.(py|sh|c|js|ts|json|txt|log|md|html|php|sql|zip|gz|tar|png|jpg|jpeg|gif|bin|so|exe|db|sqlite|yaml|yml|xml|csv|out|s|asm|o)$",
    re.IGNORECASE,
)


def _call_key(fn_name: str, fn_args: dict) -> str:
    """工具调用指纹（卡死切换器"完全重复调用"判定的输入）。

    同工具 + 同参数 → 同指纹；参数按键排序序列化，避免 dict 顺序差异误判。
    序列化失败（含不可 JSON 化的值）时退回 str(fn_args)，仍保证同参同指纹。
    """
    try:
        args = json.dumps(fn_args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = str(fn_args)
    return f"{fn_name}:{args}"


def _extract_refs(fn_name: str, fn_args: dict) -> list[str]:
    """提取一次工具调用的路径引用（依赖启发式用）。

    read_file/list_dir/write_file 直接用 path 参数；bash 从命令字符串里挑
    "形如文件路径"的 token（排除 URL 与纯命令字）。
    """
    refs: list[str] = []
    if fn_name in ("read_file", "list_dir", "write_file"):
        p = str(fn_args.get("path") or "")
        if p:
            refs.append(p)
    elif fn_name == "bash":
        cmd = str(fn_args.get("command") or "")
        try:
            toks = shlex.split(cmd)
        except (ValueError, TypeError):
            toks = cmd.split()
        for t in toks:
            if t.startswith(("http://", "https://")):
                continue  # URL 是远程地址，不是本地路径
            if "/" in t or _EXT_RE.search(t):
                refs.append(t)
    return refs


def _path_overlap(a: str, b: str) -> bool:
    """两条路径是否指向同一文件：精确串相等 或 文件名相等（兼容相对/绝对差异）。"""
    if a == b:
        return True
    na = Path(a).name
    nb = Path(b).name
    return bool(na) and na == nb


def _ratelimit_group_gate(cfg: RunConfig, group: list) -> str | None:
    """并行组启动前的组级限速门：组内 bash 命令一次性过配额检查（组级配额）。

    背景：GlobalRateLimiter.check 在单个 run_bash 入口执行，并行组内多个 bash
    同时过 check()，Semaphore 限的是"并发工具数"不是"请求率"，瞬时 rps 可超
    限速阈值——组级门把整组请求视作一次突发，先整组过配额再放行。
    方案 A 下 bash 一律单元素组，本门仅在组内 bash ≥2 时生效（防御性，也为
    方案 B 预留）；单 bash 组仍走 run_bash 入口检查（不双重计数）。
    返回 None = 放行；返回错误串 = 整组 bash 被拒（fail-closed，非 bash 照常）。
    """
    bash_cmds = [str(_args.get("command") or "") for _tc, _name, _args in group if _name == "bash"]
    if len(bash_cmds) < 2:
        return None
    rl = cfg.ratelimit
    if rl is None or not hasattr(rl, "check"):
        return None
    try:
        blocked: set[str] = set()
        for cmd in bash_cmds:
            blocked.update(rl.check(cmd))
    except Exception as e:
        # 限速器故障：观测不阻断（与 run_bash 入口检查同哲学）
        print(f"[ratelimit] 组级检查失败（放行）: {e}", file=sys.stderr)
        return None
    if blocked:
        return f"Error: 组内命令目标被限速拦截（host: {', '.join(sorted(blocked))}），请稍后再试"
    return None


def _split_parallel_groups(items: list) -> list:
    """依赖启发式分组：组内调用可并行，跨组必须等前组全部完成。

    方案 A（安全优先）并行边界：
    - 只有可静态判定无副作用的工具进并行组：read_file / list_dir / write_file
      （依赖启发式对这三类可靠）
    - bash 一律独立成组、串行执行：渗透场景 bash 多为外部请求等待，并行收益
      有限；而并行 bash 共享同一工作区 ws/ 与同一常驻容器（write_file 产物
      跨调用可见是既有设计），两个 bash 各自由脚本内部写同名文件 / outputs/
      时互踩共享目录——脚本内部 IO 对本启发式不可见，漏判即无隔离的数据
      污染，且互踩不可追溯。故按"安全优先于速度"取舍：bash 一律串行。
    - 依赖边两类（漏判只会多串行，保守安全）：
      a) 前序 write_file 产出的路径被后序调用引用（read_file 产物）
      b) 后序 write_file 覆盖组内已被引用或已产出的路径（读写同一文件）
    """
    groups: list[list] = []
    cur: list = []  # 当前无副作用并行组（仅 read_file/list_dir/write_file）
    produced: set[str] = set()  # 本组内 write_file 已写出的路径
    referenced: set[str] = set()  # 本组内调用引用过的路径

    def _flush() -> None:
        nonlocal cur, produced, referenced
        if cur:
            groups.append(cur)
        cur = []
        produced = set()
        referenced = set()

    for item in items:
        _tc, fn_name, fn_args = item
        if fn_name in ("bash", "delegate"):
            # bash / delegate 一律串行：先冲掉无副作用组，自身独占一组。
            # delegate 串行的理由同 bash：子链 run() 会覆写进程级 SECPT_JOB_ID/
            # SECPT_WORK_DIR 环境变量，并行多 delegate 会互相踩
            _flush()
            groups.append([item])
            continue
        refs = _extract_refs(fn_name, fn_args)
        dep = False
        if fn_name == "write_file":
            # 后写路径若被组内先序调用引用（会读旧版）或已被组内写出（同路径覆盖）→ 拆组
            for p in refs:
                if any(_path_overlap(p, r) for r in referenced) or any(
                    _path_overlap(p, r) for r in produced
                ):
                    dep = True
                    break
            if not dep:
                produced.update(refs)
        else:
            # 引用组内已写出的路径 → 该调用依赖前序 write_file → 拆组串行
            for p in refs:
                if any(_path_overlap(p, r) for r in produced):
                    dep = True
                    break
            if not dep:
                referenced.update(refs)
        if dep:
            _flush()
            cur = [item]
            produced = set(refs) if fn_name == "write_file" else set()
            referenced = set() if fn_name == "write_file" else set(refs)
        else:
            cur.append(item)
    _flush()
    return groups


# S3 发现启发式：凭据行 / URL（供 on_finding 上报，watchdog 据此判断是否空转）
_CRED_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_ -]?key|credential)\s*[:=]\s*(\S{4,})"
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _scan_findings(text: str, flag_re) -> list[tuple[str, str]]:
    """扫描一段文本里的发现：(kind, value)。kind ∈ flag|credential|asset。"""
    out: list[tuple[str, str]] = []
    if flag_re:
        for m in flag_re.finditer(text):
            out.append(("flag", m.group(0)))
    for m in _CRED_RE.finditer(text):
        out.append(("credential", m.group(0)))
    for m in _URL_RE.finditer(text):
        out.append(("asset", m.group(0)))
    return out


@dataclass
class RunConfig:
    job_id: str
    objective: str  # 任务全文，原文传入，不摘要
    system_extra: str  # profile 渲染出的先验+工具说明+约束
    workspace: Path
    max_steps: int = 100  # <=0 表示不限步数（7×24 长任务）
    max_wall_s: int = 900  # <=0 表示不限墙钟（7×24 长任务）
    bash_timeout_s: int = 60
    bash_whitelist: frozenset[str] = DEFAULT_BASH_WHITELIST
    flag_regex: str | None = None  # 已废弃：不再用于终止判定，仅兼容旧快照
    model_primary: str = "deepseek-chat"
    model_fallback: str = "deepseek-reasoner"
    stuck_threshold: int = 6
    stuck_increment: int = 2
    stuck_wait_cap: int = 12  # P3-11：合法重复交互（pwn 等待）的 waiting 上限
    initial_messages: list[dict] | None = None  # S1 resume：非空则直接作为初始链（含首条 system）
    round_offset: int = 0  # S1 resume：续跑后 round 编号继续，避免覆盖旧 checkpoint
    autoresume: bool = False  # 任务二：启动时自动从检查点恢复（仅顶层单任务路径开启；
    # 编排层 stage 复用同一 job_id，必须保持 False 防 stage 链串扰）。SECPT_AUTORESUME=0 全局关闭
    ratelimit: object | None = None  # S2 全局限速器（.check(command)->list[str]，非空拦截）
    # G1 目标授权范围（duck-typing：.validate(target)->None or raise，避免反向依赖 governance）
    scope: object | None = None
    # G5 审计（duck-typing：.log(event, **kw)）；治理拒绝/异常与生命周期落 JSONL
    audit: object | None = None
    context_window: int | None = None  # S3 上下文窗口（token）；None=按模型查表
    sandbox: str = "none"  # bash 执行环境：none（宿主直跑，默认）/ docker（强制）/ auto
    agent_id: str = ""  # 事件 trace：结构化日志与事件行的 agent 维度（编排层传角色名）

    # （MODEL_WINDOWS），可用 SECPT_FAKE_WINDOW 覆盖
    budget_cost_cny: float = 0.0  # S3 成本上限（元），0 表示不限
    budget_warn_pct: float = 0.8  # S3 预算警告阈值
    round_max_steps: int = 60  # S3 回合最大步数（触发 rollover）
    round_max_wall_s: int = 1500  # S3 回合最大墙钟（秒，25min）
    roe_throttle_ms: int = 0  # ROE 节速：每个 step 完成后 sleep 毫秒数（0=关闭）
    # PA4.2：本轮工具调用硬上限（0=不限）。engine_runner 按角色分档注入
    # （exploit/recon/lateral=100、verify/report=20）；超限强制进入 wrapping
    max_tool_calls: int = 0

    @property
    def work_dir(self) -> Path:
        """每个 job 一个工作目录：<workspace>/ws"""
        return Path(self.workspace) / "ws"

    @property
    def flag_re(self) -> re.Pattern | None:
        return re.compile(self.flag_regex) if self.flag_regex else None


@dataclass
class RunResult:
    flag: str | None
    steps: int
    wall_s: float
    status: str  # success | step_exceeded | wallclock_exceeded
    # | budget_exceeded | stalled | failed | error
    # stalled = 三档止损状态机收口（wrapping 期未收敛，交编排层 RESET/终态）
    messages: list[dict] = field(default_factory=list)
    cost_cny: float = 0.0  # S3 累计成本（CNY，由 BudgetTracker 记）
    # 结构化轮：prefix cache 记账随结果返回（usage 无该字段的端点恒 0）
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


# G1 scope：命令里的目标提取与回环判定（纯函数，便于单测）

_BARE_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# 裸 IPv6 候选（含 :: 压缩、[::1] 括号、内嵌 IPv4 尾巴）：宽松捕获后交 ipaddress
# 精确校验——时间串 12:34:56、MAC 地址、hex 冒号串等假阳性在校验层被剔除；
# 全 8 组 hex 形式的误报代价只是多余的 scope 拒绝（fail-closed），漏报则是绕过面
_IPV6_CAND_RE = re.compile(
    r"\[?(?:[0-9A-Fa-f]{1,4}:|:){1,7}(?:[0-9A-Fa-f]{1,4}|(?:\d{1,3}\.){3}\d{1,3})\]?"
)
# 裸域名（无 scheme）：排除常见文件扩展名，避免把 report.md / exploit.py 当目标
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}\b")
_FILE_EXT = {
    "py",
    "md",
    "json",
    "log",
    "txt",
    "sh",
    "yaml",
    "yml",
    "csv",
    "html",
    "js",
    "ts",
    "xml",
    "conf",
    "cfg",
    "pem",
    "key",
    "db",
    "sqlite",
    "zip",
    "tar",
    "gz",
    "tgz",
    "png",
    "jpg",
    "jpeg",
    "pcap",
    "pcapng",
    "pdf",
    "php",
    "bak",
}


def extract_scope_targets(command: str) -> list[str]:
    """从命令字符串提取网络目标（URL host / 裸 IP / 裸域名）。

    提取不到（纯本地命令如 pwd/ls）返回空列表 → 调用方放行。
    """
    targets: list[str] = []
    # URL：hostname 提取（含 http(s)://...）
    for m in re.finditer(r"https?://[^\s\"')]+", command):
        try:
            host = urlparse(m.group(0)).hostname
        except ValueError:
            continue
        if host:
            targets.append(host)
    # 裸 IPv4（含 host:port 形式的 host 部分）
    targets.extend(_BARE_IP_RE.findall(command))
    # 裸 IPv6（G1 补丁）：URL 形式经 urlparse.hostname 已覆盖，这里补裸/括号形式
    # （nmap -6 ::1、curl -g 'http://[2001:db8::1]' 的参数段等）——提取不到即不
    # 校验，是 v4-only 授权场景下的实际绕过面
    for m in _IPV6_CAND_RE.finditer(command):
        cand = m.group(0).strip("[]")
        try:
            ipaddress.ip_address(cand)
        except ValueError:
            continue
        targets.append(cand)
    # 裸域名：末段是文件扩展名的跳过；URL 已提取的 host 去重
    for m in _DOMAIN_RE.findall(command):
        if m.rsplit(".", 1)[-1] in _FILE_EXT:
            continue
        targets.append(m)
    # 去重保序 + 小写归一
    seen: set[str] = set()
    uniq = []
    for t in targets:
        t = t.strip().lower()
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# RFC 2606/6761 保留域（金丝雀/占位 URL 的典型形态；scope 拒绝时给专属提示）
_RESERVED_TLDS = (".example", ".invalid", ".test", ".localhost")
_RESERVED_DOMAINS = {"example.com", "example.net", "example.org", "localhost"}


def is_reserved_target(target: str) -> bool:
    """保留域判定：example/invalid/test 等 TLD 与 example.* 二级域。"""
    t = target.strip().lower().rstrip(".")
    if not t:
        return False
    return t in _RESERVED_DOMAINS or t.endswith(_RESERVED_TLDS)


def is_loopback_target(target: str) -> bool:
    """本地回环一律放行：渗透探测 SSRF 时请求 127.0.0.1:3000 是合法行为。"""
    t = target.strip().lower()
    if t in ("localhost",) or t.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(t).is_loopback
    except ValueError:
        return False


def _scope_check(cfg: RunConfig, command: str) -> str | None:
    """G1 目标授权校验。返回错误消息 = 拒绝；None = 放行。

    语义（与 intercept 故障处理哲学一致）：
    - 未命中白名单 → fail-closed 拒绝 + audit(scope.denied)
    - 校验器自身异常 → fail-open 放行 + stderr 告警 + audit(scope.error)
    - SECPT_SCOPE=0 或 cfg.scope 未装配 → 直接放行
    """
    scope = cfg.scope
    if scope is None or os.environ.get("SECPT_SCOPE", "1") == "0":
        return None
    try:
        targets = [t for t in extract_scope_targets(command) if not is_loopback_target(t)]
    except Exception as e:  # 提取层异常不影响执行（fail-open）
        print(f"[scope] 目标提取失败（放行）: {e}", file=sys.stderr)
        return None
    if not targets:
        return None
    try:
        for t in targets:
            scope.validate(t)
        return None
    except Exception as e:
        if getattr(e, "scope_denied", False):
            # 治理拒绝（ScopeError）：fail-closed + audit
            if cfg.audit is not None:
                with contextlib.suppress(Exception):
                    cfg.audit.log("scope.denied", target=str(e)[:200], command=command[:200])
            # 保留域专属提示（复评修复）：benchmark 金丝雀/占位外带地址尝试会被拦，
            # 旧消息只教"设 SECPT_SCOPE_ALLOW"——对保留域是错误引导（实测模型据此
            # 逐字分析 scope 语义烧了十来个步数）
            _reserved = [t for t in targets if is_reserved_target(t)]
            _hint = (
                "\n⚠ "
                + "、".join(_reserved[:3])
                + " 属保留域/测试域（example/invalid/test 等）——常见于平台金丝雀或占位"
                "外带地址，不要对其发起请求（含 DNS 探测与脚本内请求）；"
                "外带类验证请使用平台下发的 container_addr 或题面明确指定的通道。"
                if _reserved
                else ""
            )
            return f"Error: {e}（目标必须在授权范围内；如需扩展设置 SECPT_SCOPE_ALLOW）{_hint}"
        # 校验器自身异常：fail-open + audit（观测治理层不阻断主链路）
        print(f"[scope] 求值失败（放行）: {e}", file=sys.stderr)
        if cfg.audit is not None:
            with contextlib.suppress(Exception):
                cfg.audit.log("scope.error", error=str(e)[:200], command=command[:200])
        return None


# ---------------------------------------------------------------------------
# 插件网络工具治理（P1）：Python/YAML 插件的网络类工具（浏览器等）此前完全
# 绕过 scope/ratelimit/intercept——run_bash 的治理前置只保护内置 bash。
# 插件 schema 声明 x-secpt-governance: {"network": true} 后，Engine 装配时
# 统一套治理前置：ratelimit（跨 job 配额）→ scope（复用 extract_scope_targets，
# fail-closed）→ intercept（ask 阻塞放线程池，不冻结事件循环）→ 原工具。
# 声明键是 engine 私有协议，发往 LLM 的请求里一律剥离（_public_tool_schema）。
# ---------------------------------------------------------------------------

_GOV_KEY = "x-secpt-governance"


def _extract_governance(schema: dict) -> dict:
    """提取插件治理声明（兼容 schema 三种形态：裸 parameters / function / 顶层）。"""
    if not isinstance(schema, dict):
        return {}
    fn = schema.get("function") if isinstance(schema.get("function"), dict) else {}
    params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
    for cand in (schema.get(_GOV_KEY), fn.get(_GOV_KEY), params.get(_GOV_KEY)):
        if isinstance(cand, dict):
            return cand
    return {}


def _public_tool_schema(schema: dict) -> dict:
    """剥离 x- 私有声明键（engine 协议，不进 LLM 请求）。"""
    out = {k: v for k, v in schema.items() if not k.startswith("x-")}
    fn = out.get("function")
    if isinstance(fn, dict):
        fn = {k: v for k, v in fn.items() if not k.startswith("x-")}
        params = fn.get("parameters")
        if isinstance(params, dict):
            fn["parameters"] = {k: v for k, v in params.items() if not k.startswith("x-")}
        out["function"] = fn
    return out


def _governed_plugin_fn(cfg: RunConfig, tool_name: str, fn):
    """网络类插件工具的治理包装：ratelimit → scope → intercept → 原工具（同 run_bash 语义）。"""

    async def _wrapped(args: dict) -> str:
        text = " ".join(str(v) for v in (args or {}).values() if isinstance(v, (str, int, float)))
        if text:
            # 全局限速（与 run_bash 入口同语义：跨 job 配额，非空 = 拦截；限速器
            # 自身故障 fail-closed——与 run_bash 一致，防治理失效时打穿目标）
            rl = cfg.ratelimit
            if rl is not None and hasattr(rl, "check"):
                try:
                    blocked = rl.check(text)
                    if blocked:
                        return f"Error: rate limit exceeded for {blocked}（global max_rps）"
                except Exception as e:
                    return f"Error: ratelimit check failed: {e}"
            scope_err = _scope_check(cfg, text)
            if scope_err:
                return scope_err
            if os.environ.get("SECPT_INTERCEPT", "1") != "0":
                try:
                    from secpt.governance.intercept import Interceptor

                    ok, why = await asyncio.to_thread(
                        Interceptor().eval, tool=tool_name, command=text, job_id=cfg.job_id
                    )
                    if not ok:
                        if cfg.audit is not None:
                            with contextlib.suppress(Exception):
                                cfg.audit.log(
                                    "intercept.denied", command=text[:200], note=why[:200]
                                )
                        return why
                except Exception as e:
                    print(f"[intercept] 求值失败（放行）: {e}", file=sys.stderr)
        result = fn(args)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, str) else str(result)

    return _wrapped


def build_tools_doc(cfg: RunConfig) -> str:
    _auto_shell = os.environ.get("SECPT_BASH_AUTO_SHELL", "0") == "1"
    if _auto_shell:
        _bash_desc = (
            "含未引用管道 |、重定向 >、分号 ;、后台 &、命令替换 $( )/反引号时，"
            "整条命令自动交给 bash -c 执行（引号内元字符仍是参数内容；$VAR 会展开）。"
        )
        _shell_note = ""
    else:
        _bash_desc = (
            "不支持未引用的管道 |、重定向 >、分号 ;、后台/串联 &、命令替换 $( ) 与反引号。"
            "引号内的元字符只是参数内容、可以直接用；裸 $VAR 与通配符 * 会按字面传给命令"
            "（argv 直传不展开）。"
        )
        _shell_note = (
            "确需组合逻辑/变量展开时，先用 write_file 写 Python 脚本到工作目录，"
            "再用 python3 执行（bash/sh 仅在其被加入白名单时可用）。"
        )
    return (
        f"- bash(command)  白名单命令：{', '.join(sorted(cfg.bash_whitelist))}，"
        f"超时{cfg.bash_timeout_s}s。command 必须是字符串（不支持数组），"
        f"内部按空格拆成参数直接执行（不经 shell）：{_bash_desc}{_shell_note}"
        "宿主机绝对路径（如 /home/kali/...）在沙箱容器内不可见，一律用相对路径。"
        f"工作目录：{cfg.work_dir}，所有相对路径都自动落到它下面\n"
        f"- read_file(path, offset?, limit?)  读文件（相对路径=工作目录下）；"
        f"分页读：offset 字符偏移、limit 字符数（缺省 {BASH_MAX_OUT}，上限 {READ_SLICE_MAX}），"
        f"未读完会提示续读 offset；文件上限 {READ_HARD_CAP // (1024 * 1024)}MB\n"
        "- list_dir(path)  列目录（相对路径=工作目录下），深度1\n"
        f"- write_file(path, content)  写文件到 {cfg.work_dir}/ 下"
        "（相对路径自动落到工作目录，禁止越界；可写 python 脚本再 bash 执行）\n"
        "- 目标授权：网络命令的目标（URL/IP/域名）必须在授权范围内，"
        "范围外会被拒绝执行；本地回环（127.0.0.1/localhost）不受限。"
        "如需访问额外目标，请告知操作者设置 SECPT_SCOPE_ALLOW（逗号分隔 IP/CIDR/域名）\n"
        f"- 并行：同轮可发起最多 {_parallel_max()} 个相互独立的工具调用"
        "（系统只并行执行无副作用调用 read_file/list_dir/write_file；"
        "bash 一律串行执行——共享工作区 ws/，并行会互踩文件）；"
        "依赖彼此结果的调用请分轮串行"
    )


def build_tool_schemas(cfg: RunConfig) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": f"执行白名单命令，超时{cfg.bash_timeout_s}s，输出截断8000字符",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    f"读取文件（支持分页），单次上限 {READ_SLICE_MAX} 字符；"
                    "未读完返回续读提示（用 offset 翻页）"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "description": "起始字符偏移（缺省 0）"},
                        "limit": {
                            "type": "integer",
                            "description": f"读取字符数（缺省 {BASH_MAX_OUT}，上限 {READ_SLICE_MAX}）",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "列目录，深度1层",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": f"写文件到 {cfg.work_dir}/ 下",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
    ]


def make_tool_fns(cfg: RunConfig) -> dict:
    """四个工具：bash / read_file / list_dir / write_file。闭包绑定 cfg。"""
    work_dir = cfg.work_dir

    _out_seq = [0]  # 长输出落盘序号（闭包内用容器保持跨调用状态）

    def _truncate_output(text: str) -> str:
        """输出超 BASH_MAX_OUT 时全量落盘，返回截断文本+引用路径。

        ARTEX 长日志落盘：扫描产生的超长输出截断给模型，完整内容保存到
        工作目录 outputs/ 下，模型可随时 read_file 按需取细节（避免窗口浪费）。
        """
        if not text.strip():
            return "(no output)"
        if len(text) <= BASH_MAX_OUT:
            return text
        _out_seq[0] += 1
        odir = Path(work_dir) / "outputs"
        odir.mkdir(parents=True, exist_ok=True)
        fname = f"bash-{_out_seq[0]}.log"
        (odir / fname).write_text(text, encoding="utf-8", errors="replace")
        # B3：头 + 尾 + 路径——只给头部会丢掉扫描结果末尾的关键命中（真实故障：
        # nmap/目录扫描的"最后几行"往往正是 flag 或 200 响应），尾部 2KB 兜住收尾信息
        _tail_n = 2000
        head = text[: BASH_MAX_OUT - _tail_n]
        tail = text[-_tail_n:]
        return (
            f"{head}\n...（中间省略 {len(text) - (_tail_n + len(head))} 字符；"
            f"完整输出已存 outputs/{fname}，用 read_file(offset/limit) 或 grep 取细节）...\n"
            f"{tail}"
        )

    def _ratelimit_check(command: str) -> str | None:
        # S2 全局限速：网络请求先过配额检查（跨 job 生效）
        rl = cfg.ratelimit
        if rl is not None and hasattr(rl, "check"):
            try:
                blocked = rl.check(command)
                if blocked:
                    return f"Error: rate limit exceeded for {blocked}（global max_rps）"
            except Exception as e:
                return f"Error: ratelimit check failed: {e}"
        return None

    async def run_bash(command: str) -> str:
        """白名单 argv 执行（禁 shell=True）。需要管道/重定向时引导写脚本再执行；
        仅当 profile 显式把 bash/sh 加入白名单时才允许 shell 解释（bash -c）。"""
        rl_err = _ratelimit_check(command)
        if rl_err:
            return rl_err
        try:
            if isinstance(command, (list, tuple)):  # 模型误传数组时兜底拼接
                command = shlex.join(str(c) for c in command)
            parts = shlex.split(command)
        except (ValueError, AttributeError, TypeError) as e:
            return f"Error: 命令解析失败（command 必须是字符串）: {e}"
        if not parts:
            return "Error: empty command"
        exe = parts[0]
        # P0-3：元字符扫描先于白名单检查——否则 "pwd;" 会报"pwd 不在白名单"，
        # 错误提示与真实原因（shell 组合构造）不符，模型反复重试烧步数。
        # benchmark 例外：SECPT_BASH_AUTO_SHELL=1 且 bash/sh 在白名单时，整条命令
        # 自动交给 bash -c（模型不必为每个管道写临时脚本）；scope/intercept 仍按
        # 原始 command 校验。
        uses_shell = exe in _SHELL_EXES
        if not uses_shell and _has_unquoted_shell_chain(command):
            _auto_shell = os.environ.get("SECPT_BASH_AUTO_SHELL", "0") == "1"
            _shell_exe = next((s for s in _SHELL_EXES if s in cfg.bash_whitelist), "")
            if _auto_shell and _shell_exe:
                exe = _shell_exe
                uses_shell = True
            else:
                return (
                    "Error: 命令含未引用的 shell 组合构造（| ; & > < 或 $( ) / 反引号），"
                    "当前按参数直传执行、不经 shell 解释。引号内的元字符只是参数内容、"
                    "可以直接用；裸 $VAR 与通配符 * 会按字面传给命令（argv 不展开）。\n"
                    "改写示例：① 分号串联 → 拆成多条独立 bash 调用（'a; b' 改为先跑 a 再跑 b）；"
                    "② 管道/重定向/循环/变量展开 → 用 write_file 写 Python 脚本，"
                    "再用 python3 执行（bash/sh 不在当前白名单时不能用 bash 跑脚本）。"
                )
        if exe not in cfg.bash_whitelist:
            # 常见摩擦命令：不裸拒，给可行动的替代用法
            if exe == "cd":
                hint = "不要用 cd：bash 的工作目录就是你的工作目录。"
            elif exe == "timeout":
                hint = (
                    f"工具已自带超时（{cfg.bash_timeout_s}s）；脚本内需要限时请 write_file 写脚本。"
                )
            elif exe == "python":
                hint = "白名单命令是 python3（无 python 别名）。"
            else:
                hint = ""
            return (
                f"Error: '{exe}' 不在白名单。可用命令：{sorted(cfg.bash_whitelist)}。"
                f"{hint}遍历文件用 find 或 ls -R；复杂逻辑用 write_file 写脚本再执行。"
            )
        argv: list[str] = [exe, "-c", command] if uses_shell else parts

        # G1 目标授权范围：白名单/元字符之后、intercept 之前（SECPT_SCOPE=0 全局关）。
        # 未命中 → fail-closed 拒绝；校验器自身异常 → fail-open 放行（见 _scope_check）
        scope_err = _scope_check(cfg, command)
        if scope_err:
            return scope_err

        # 人在环路-审批：白名单/元字符之后、执行之前（SECPT_INTERCEPT=0 可全局关）。
        # eval 的 ask 分支会阻塞等待人工审批（最长 timeout_s，默认 300s）——必须放
        # 线程池执行，同步等待会冻结整个事件循环（同进程其他 worker 全部停摆）
        if os.environ.get("SECPT_INTERCEPT", "1") != "0":
            try:
                from secpt.governance.intercept import Interceptor

                ok, why = await asyncio.to_thread(
                    Interceptor().eval, tool="bash", command=command, job_id=cfg.job_id
                )
                if not ok:
                    # G5 审计：拦截事件（超时拒绝单列，便于区分人工 deny 与审批超时）
                    if cfg.audit is not None:
                        with contextlib.suppress(Exception):
                            _ev = (
                                "intercept.timeout"
                                if ("超时" in why or "timeout" in why.lower())
                                else "intercept.denied"
                            )
                            cfg.audit.log(_ev, command=command[:200], note=why[:200])
                    return why
            except Exception as e:
                print(f"[intercept] 求值失败（放行）: {e}", file=sys.stderr)
                if cfg.audit is not None:
                    with contextlib.suppress(Exception):
                        cfg.audit.log("intercept.error", error=str(e)[:200], command=command[:200])

        # 流量录制（B3）代理 env：宿主与容器两条执行路径共用
        proxy_env: dict | None = None
        _proxy = os.environ.get("SECPT_PROXY_URL")
        if _proxy:
            proxy_env = {
                "HTTP_PROXY": _proxy,
                "HTTPS_PROXY": _proxy,
                "http_proxy": _proxy,
                "https_proxy": _proxy,
            }
            ca = os.environ.get("SECPT_PROXY_CA")
            if ca:
                proxy_env.update(
                    {
                        "SSL_CERT_FILE": ca,
                        "REQUESTS_CA_BUNDLE": ca,
                        "NODE_EXTRA_CA_CERTS": ca,
                        "CURL_CA_BUNDLE": ca,
                    }
                )

        # Docker 沙箱路径：容器内执行（宿主隔离）；auto 且 docker 不可用 → 回退宿主
        if cfg.sandbox != "none":
            from secpt.runtime.sandbox import SandboxUnavailable, sandbox_scope

            try:
                with sandbox_scope(cfg.job_id, work_dir=str(work_dir), mode=cfg.sandbox) as _sb:
                    if _sb is not None:
                        rc, out_b, err_b = await asyncio.to_thread(
                            _sb.exec, argv, timeout_s=cfg.bash_timeout_s, env=proxy_env
                        )
                        text = out_b + ("\n" + err_b if err_b else "")
                        if rc == 124:
                            return f"Error: timeout {cfg.bash_timeout_s}s"
                        if rc != 0 and not text.strip():
                            return f"Error: exit {rc}"
                        return _truncate_output(text)
            except SandboxUnavailable as e:
                if cfg.sandbox == "docker":
                    return f"Error: sandbox 不可用: {e}"
                # auto 模式：容器起不来 → 回退宿主执行（观测层不阻断主链路），
                # 告警只打一次，错误信息截断防刷屏
                if not getattr(run_bash, "_sb_warned", False):
                    run_bash._sb_warned = True
                    print(
                        f"[engine] 沙箱不可用，回退宿主执行: {str(e).splitlines()[0][:120]}",
                        file=sys.stderr,
                    )

        env = os.environ.copy()
        if proxy_env:
            env.update(proxy_env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(work_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            return f"Error: 启动失败: {e}"

        async def _read_capped(
            stream: asyncio.StreamReader | None, cap: int = BASH_READ_CAP
        ) -> bytes:
            if stream is None:
                return b""
            data = bytearray()
            while len(data) < cap:
                try:
                    chunk = await stream.read(65536)
                except Exception:
                    break
                if not chunk:
                    break
                data.extend(chunk)
            return bytes(data)

        try:
            out_b, err_b = await asyncio.wait_for(
                asyncio.gather(_read_capped(proc.stdout), _read_capped(proc.stderr)),
                timeout=cfg.bash_timeout_s,
            )
            await proc.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return f"Error: timeout {cfg.bash_timeout_s}s"
        out = out_b.decode(errors="replace")
        err = err_b.decode(errors="replace")
        text = out + ("\n" + err if err else "")
        if proc.returncode != 0 and not text.strip():
            return f"Error: exit {proc.returncode}"
        if not text.strip() and proc.returncode == 0:
            return "(no output)"
        return _truncate_output(text)

    def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
        """读文件（C：支持分页）。

        offset：起始字符偏移（缺省 0）；limit：读取字符数（0=默认 8000，上限
        READ_SLICE_MAX）。返回切片；未读完附续读提示（模型据此翻页，不再靠
        head/tail 拼）。文件超 READ_HARD_CAP 拒绝（引导 bash head/tail/sed 分段）。
        """
        p = Path(path)
        if not p.is_absolute():
            p = work_dir / p  # 与 bash cwd / write_file 一致
        try:
            sz = p.stat().st_size
            if sz > READ_HARD_CAP:
                return (
                    f"Error: file too large ({sz} > {READ_HARD_CAP})；"
                    "用 bash 的 head/tail/sed -n 分段查看大文件"
                )
            text = p.read_text(errors="replace")
        except Exception as e:
            return f"Error: {e}"
        try:
            off = max(0, int(offset or 0))
            lim = int(limit) if limit else BASH_MAX_OUT
            lim = max(1, min(lim, READ_SLICE_MAX))
        except (TypeError, ValueError):
            return "Error: offset/limit 必须是整数"
        total = len(text)
        if not text:
            return "(empty file)"
        if off >= total:
            return f"Error: offset {off} 超出文件长度 {total}"
        chunk = text[off : off + lim]
        end = off + len(chunk)
        if end < total:
            chunk += f"\n...(已读 {off}-{end}/{total} 字符，续读: read_file(path, offset={end}))"
        return chunk

    def list_dir(path: str) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = work_dir / p  # 与 bash cwd / write_file 一致
        try:
            lines = []
            for e in sorted(p.iterdir()):
                sz = e.stat().st_size if e.is_file() else 0
                tag = "/" if e.is_dir() else ""
                lines.append(f"  {e.name}{tag}  {sz}")
            return f"{path} ({len(lines)} items):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    def write_file(path: str, content: str) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = work_dir / p  # 相对路径一律落到工作目录
        try:
            pr = p.resolve()
            wr = work_dir.resolve()
            if not (str(pr) == str(wr) or str(pr).startswith(str(wr) + os.sep)):
                return (
                    f"Error: 只能写到 {wr}/ 下。你传的是 {path!r}；"
                    f"直接给文件名（如 exploit.py）即可，会自动落到工作目录。"
                )
        except Exception:
            return f"Error: bad path {path!r}"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"OK: wrote {len(content)} bytes to {p}"
        except Exception as e:
            return f"Error: {e}"

    return {
        "bash": lambda a: run_bash(a["command"]),
        "read_file": lambda a: read_file(a["path"], a.get("offset", 0), a.get("limit", 0)),
        "list_dir": lambda a: list_dir(a["path"]),
        "write_file": lambda a: write_file(a["path"], a["content"]),
    }


# ── 运行时工具升级（request_tools）：攻击面是滚动的，装配不能是一次性的 ──
_REQUEST_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_tools",
        "description": (
            "运行中按需启用备选工具（立即生效，无需重启）：发现题面未提的新攻击面时用——"
            "如出现二进制（analyze_binary/pwn_checksec）、需要常驻会话驱动 gdb（session）、"
            "需要带外验证（oob）、需要结构化扫描（recon_parse）。一次最多 4 个，附一句话理由。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要启用的工具名（必须是池内工具，schema 里有清单）",
                },
                "reason": {"type": "string", "description": "为什么需要（审计留痕）"},
            },
            "required": ["names"],
        },
    },
}
_POOL_PER_CALL_MAX = 4  # 单次 request_tools 启用上限（防一次全池挂载撑爆 schema）


# ── bash 扫描输出自动结构化：agent 用裸 nmap/ffuf 时图记忆也不断粮 ──
_SCANNER_CMD_RE = re.compile(
    r"\b(nmap|masscan|ffuf|gobuster|dirsearch|whatweb|nikto|wpscan|nuclei|httpx)\b"
)
_AUTO_PARSER: object | None = None


def _get_auto_parser():
    """懒加载 recon_parse 处理器（只加载一次；不可用返回 None，不再反复扫描）。"""
    global _AUTO_PARSER
    if _AUTO_PARSER is None:
        try:
            from secpt.tools.plugin_loader import engine_tools_for

            _AUTO_PARSER = engine_tools_for(["recon_parse"]).get("recon_parse", (None, None))[1]
        except Exception:
            _AUTO_PARSER = False
    return _AUTO_PARSER or None


def _auto_structured_bash(cfg: RunConfig, bash_fn, parser=None):
    """bash 包装：扫描类命令的输出自动喂给 recon_parse 落 ws/assets.json（best-effort）。

    解决"工具契约靠自觉"——模型裸跑扫描器而不调 recon_parse 时，资产图就断粮。
    解析失败绝不影响 bash 本身（观测层纪律）；SECPT_AUTO_STRUCTURE=0 可关。
    parser 参数仅供测试注入。
    """

    async def _wrapped(args: dict) -> str:
        result = bash_fn(args)
        if inspect.isawaitable(result):
            result = await result
        cmd = str((args or {}).get("command") or "")
        if not _SCANNER_CMD_RE.search(cmd) or len(result) < 40:
            return result
        try:
            fn = parser or _get_auto_parser()
            if fn is None:
                return result
            out_dir = cfg.work_dir / "outputs"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"auto-struct-{int(time.time() * 1000)}.txt"
            path.write_text(result, encoding="utf-8")
            summary = fn({"path": str(path), "format": "auto", "merge": True})
            if isinstance(summary, str) and not summary.startswith("Error"):
                return (
                    result
                    + f"\n[auto-structure] 扫描输出已自动解析入 ws/assets.json（{summary[:120]}）"
                )
        except Exception:
            pass
        return result

    return _wrapped


class Engine:
    """单链 function-calling 主循环。llm 需实现 call(messages, tools, model)；
    tools 为 {name: fn(args)->str}；hooks 为可选回调（见 run 内调用点）；
    summarizer 为六段式摘要器（async (objective, messages)->str），默认按
    SECPT_FAKE_LLM 自动选 stub / 真实 deepseek-chat。

    extra_tools: 插件工具 {name: (schema, fn)}，与内置四工具合并注册
    （schema 为 OpenAI function schema，fn 为 async/sync fn(args)->str）。
    verbose: 打印步进详情（默认 False，仅关键行输出到 stderr）。
    """

    def __init__(
        self,
        cfg: RunConfig,
        llm,
        tools: dict | None = None,
        hooks=None,
        summarizer=None,
        extra_tools: dict | None = None,
        verbose: bool = False,
        enable_delegate: bool | None = None,
        tool_pool: dict | None = None,
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.tools = tools if tools is not None else make_tool_fns(cfg)
        self.extra_tools = extra_tools or {}
        # delegate 子智能体工具（市场标准形态；默认关，SECPT_DELEGATE=1 开启）。
        # 子链由 make_delegate_tool 以 enable_delegate=False 构造——结构性禁递归
        if enable_delegate is None:
            enable_delegate = os.environ.get("SECPT_DELEGATE", "0") == "1"
        if enable_delegate and "delegate" not in self.extra_tools:
            _schema, _fn = make_delegate_tool(cfg, llm)
            self.extra_tools["delegate"] = (_schema, _fn)
        # 备选工具池：不默认挂载（省 schema token），agent 发现新攻击面时用
        # request_tools 运行时启用——修复"工具装配一次性、攻击面滚动"的错配
        self._tool_pool: dict = dict(tool_pool or {})
        if self._tool_pool:
            self.extra_tools["request_tools"] = (
                _REQUEST_TOOLS_SCHEMA,
                self._make_request_tools_fn(),
            )
        # bash 扫描输出自动结构化（SECPT_AUTO_STRUCTURE=0 关闭）：裸扫描也喂资产图
        if os.environ.get("SECPT_AUTO_STRUCTURE", "1") == "1" and "bash" in self.tools:
            self.tools["bash"] = _auto_structured_bash(cfg, self.tools["bash"])
        self._extra_schemas: list[dict] = []
        for name, (schema, fn) in self.extra_tools.items():
            if _extract_governance(schema).get("network"):
                # 网络类插件工具（浏览器等）：装配时统一套治理前置（scope→intercept）
                fn = _governed_plugin_fn(cfg, name, fn)
            self.tools[name] = fn
            self._extra_schemas.append(_public_tool_schema(schema))
        self.hooks = hooks
        self.summarizer = summarizer
        self.verbose = verbose
        # 引擎侧 finding 去重（避免同一条 URL/凭据刷屏事件流）
        self._finding_seen: set[tuple[str, str]] = set()
        # 非阻塞 compaction 状态：待回填占位摘要的 k 集合 + 后台摘要 worker 任务
        self._pending_summary_ks: set[int] = set()
        self._bg_task: asyncio.Task | None = None
        # PA2（PentAGI Mentor 思想）：卡死决策点用强模型诊断+换路线，替代固定文案。
        # 默认关（SECPT_MENTOR=1 开）；模型取 SECPT_MENTOR_MODEL > SECPT_ESCALATE_MODEL；
        # 未配模型/假 LLM/配额耗尽/调用失败 → 静默回退固定文案（best-effort 纪律不破）
        self._mentor_enabled = os.environ.get("SECPT_MENTOR", "0") == "1"
        self._mentor_model = (
            os.environ.get("SECPT_MENTOR_MODEL", "").strip()
            or os.environ.get("SECPT_ESCALATE_MODEL", "").strip()
        )
        try:
            self._mentor_max_calls = max(0, int(os.environ.get("SECPT_MENTOR_MAX_CALLS", "2") or 2))
        except ValueError:
            self._mentor_max_calls = 2

    def _make_request_tools_fn(self):
        """request_tools 处理器：从备选池启用工具（只增不减、单次上限、审计留痕）。

        网络类工具经池启用时套与初始装配同款的治理前置（scope→intercept）。
        """

        def _fn(args: dict) -> str:
            names = args.get("names") or []
            if isinstance(names, str):
                names = [names]
            reason = str(args.get("reason") or "").strip()
            unknown = [str(n) for n in names if n not in self._tool_pool]
            newly: list[str] = []
            for n in names:
                if len(newly) >= _POOL_PER_CALL_MAX:
                    break
                if n not in self._tool_pool:
                    continue
                schema, fn = self._tool_pool.pop(n)
                if _extract_governance(schema).get("network"):
                    fn = _governed_plugin_fn(self.cfg, n, fn)
                self.tools[n] = fn
                self._extra_schemas.append(_public_tool_schema(schema))
                newly.append(str(n))
            if not newly:
                return (
                    f"Error: 请求的工具不在池内或已启用: {unknown}；"
                    f"池剩余: {sorted(self._tool_pool) or '空'}"
                )
            note = f"（未在池内: {unknown}）" if unknown else ""
            return (
                f"已启用 {len(newly)} 个工具: {', '.join(newly)}，schema 已注入下一轮请求。"
                f"理由: {reason[:80] or '未填写'}{note}。池剩余: {sorted(self._tool_pool) or '空'}"
            )

        return _fn

    async def _mentor_advice(
        self, objective: str, actions: list, stuck_signal: str, findings
    ) -> str | None:
        """PA2 导师诊断：输入最近动作/卡死信号/已确认发现，返回换路线建议。

        失败/超时(45s)/空返回一律返回 None（调用方回退固定文案，best-effort）。
        tools=None 区分导师调用与主链调用（同一 self.llm 双用，签名兼容两种形态）。
        """
        lines = [f"- step{s} {t} {a} → {r}" for (s, t, a, r) in actions]
        findings_txt = "；".join(f"{k}:{v[:60]}" for k, v in list(findings)[:6]) or "无"
        prompt = (
            "【导师介入】渗透/解题任务卡死诊断。只输出：1) 卡点最可能的原因（一句话）；"
            "2) 换路线的具体建议 2-3 条（可直接执行）。禁止复述已知信息，总长 ≤200 字。\n\n"
            f"任务目标：{objective[:400]}\n"
            "最近动作（时间序，最多 10 条）：\n"
            + ("\n".join(lines) or "- 无")
            + f"\n卡死信号：{stuck_signal[:200]}\n"
            f"已确认发现：{findings_txt}"
        )
        req = [{"role": "user", "content": prompt}]
        try:
            if hasattr(self.llm, "call"):
                resp = await asyncio.wait_for(
                    self.llm.call(req, None, self._mentor_model), timeout=45.0
                )
            else:
                resp = await asyncio.wait_for(self.llm(req, None, self._mentor_model), timeout=45.0)
        except Exception:
            counter_inc("mentor_calls_total", status="error")
            return None
        try:
            advice = (resp.choices[0].message.content or "").strip()
        except Exception:
            return None
        if not advice:
            return None
        if getattr(resp, "usage", None) is not None:
            counter_inc("mentor_tokens_total")  # token 明细随结构化日志留痕
        return advice

    async def _compaction_worker(self, log) -> None:
        """后台摘要 worker：拉取 pending 请求生成六段摘要（不阻塞主循环）。

        崩溃/取消时残留请求保留在 pending 队列，由 resume 后的下一轮 run 或
        `secpt worker summarize` 兜底处理；摘要文件（<k>.summary.md）是完成真相源。
        """
        from secpt.core.context.compaction import (
            complete_request_summary,
            read_pending_requests,
        )
        from secpt.core.job.resume import jobs_dir

        while True:
            reqs = read_pending_requests(jobs_dir(), self.cfg.job_id)
            if not reqs:
                await asyncio.sleep(0.5)
                continue
            for path, req in reqs:
                try:
                    await complete_request_summary(path, req, self.summarizer, self.hooks)
                    log.info(
                        "engine.compact_completed",
                        k=int(req.get("k") or 0),
                        removed=int(req.get("removed_n") or 0),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # 单请求失败不拖垮 worker（请求留在队列重试）
                    log.warning("engine.compact_worker_error", error=str(e))
                    await asyncio.sleep(1.0)

    async def run(self) -> RunResult:
        cfg = self.cfg
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        configure_logging()
        # 插件上下文注入：local_runner / analyze_binary 等经环境变量定位
        # job 审计文件与 work_dir（outputs 落盘基准）；SECPT_VAR 若上层已设
        # （如 benchmark 导出）则尊重不覆盖
        os.environ["SECPT_JOB_ID"] = cfg.job_id
        os.environ["SECPT_WORK_DIR"] = str(cfg.work_dir)
        os.environ.setdefault("SECPT_VAR", "var")
        # trace 维度：job_id 必有；agent_id 非空才 bind（编排层传角色名，JSON 日志可按 agent 检索）
        _agent = {"agent": cfg.agent_id} if cfg.agent_id else {}
        log = _log.bind(job_id=cfg.job_id, **_agent)

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")

        # 事件驱动恢复：无显式 initial_messages 且存在存档（rounds+events.jsonl）
        # 时自动从检查点续跑——round/预算/detector/steps 全部接续，防重启绕预算。
        # SECPT_AUTORESUME=0 关闭（想对同 job_id 全新重跑时用）。
        _checkpoint = None
        if (
            cfg.autoresume
            and cfg.initial_messages is None
            and os.environ.get("SECPT_AUTORESUME", "1") != "0"
        ):
            try:
                from secpt.core.job.resume import load_last_checkpoint

                _checkpoint = load_last_checkpoint(cfg.job_id)
            except Exception as e:
                log.warning("engine.autoresume_failed", error=str(e))
            if _checkpoint:
                cfg.initial_messages = _checkpoint["messages"]
                cfg.round_offset = _checkpoint["round"]
                log.info(
                    "engine.autoresume",
                    round=_checkpoint["round"],
                    steps=_checkpoint["steps"],
                    cost_cny=_checkpoint["cost_cny"],
                    nudges=_checkpoint["nudges"],
                )

        # system prompt：system_extra 已含先验+工具说明+约束
        system = cfg.system_extra + (
            "\n\n输出要求：先用一句话说明你的判断，再调用工具；拿到 flag 后输出 ANSWER: <flag 内容>"
        )
        if cfg.initial_messages:
            # S1 resume：直接用已存档的链继续（含首条 system），保 prefix cache
            messages = [dict(m) for m in cfg.initial_messages]
        else:
            messages: list[dict] = [{"role": "system", "content": system}]

        tool_schemas = build_tool_schemas(cfg) + self._extra_schemas
        flag_re = cfg.flag_re

        model = cfg.model_primary
        steps = 0
        cache_hit_tokens = 0  # A：usage 带 cache 字段时累计（resume 后从 0 起，近似口径）
        cache_miss_tokens = 0
        t0 = time.time()
        flag_found = None
        status = "failed"  # 默认值：循环条件不满足且未显式设置时兜底
        last_tool_has_new_info = True
        detector = StuckDetector(
            cfg.stuck_threshold, cfg.stuck_increment, wait_cap=cfg.stuck_wait_cap
        )

        # ---- 三档止损状态机（设计 §2）：RUNNING→NUDGED→WRAPPING→STALLED ----
        # 与 detector 的职责分界：detector 管"检测"（四触发器不变），这里管"档位"
        # （nudge 后无改善→收尾注入→判 stalled 优雅退出）。STALLED 不抛异常、
        # 正常落 transcript/rounds，交编排层决定 RESET 或终态（红线：治理链不受
        # 循环状态影响，WRAPPING 只是追加 user 消息 + 记账，不锁工具）。
        loop_enabled = _loop_stop_enabled()
        loop_phase = "running"  # running | nudged | wrapping
        steps_since_nudge = 0  # nudge 后无结构性改善的步数（有改善清零）
        wrap_steps = 0  # 进入 wrapping 后的步数（>= STALL_AFTER_WRAP 判 stalled）
        wrap_reason = ""  # 进入 wrapping 的原因（nudge_no_improve/budget_pct/no_yield/tool_cap）
        _tool_calls_done = 0  # PA4.2：本轮已执行工具调用数（cfg.max_tool_calls 上限）
        _tc_parse_fail_streak = 0  # PA4.1：连续 arguments JSON 解析失败计数（成功清零）
        _tc_rescue_shown = False  # PA4.1：自救提示每个连续失败 episode 只发一次
        mentor_calls = 0  # PA2：导师调用配额计数（≤ SECPT_MENTOR_MAX_CALLS，默认 2）
        _margin_win: deque = deque(maxlen=_margin_window())
        _margin_findings = 0  # 窗口内成果数滚动和（finding=产出，不是"新信息"）
        _margin_cost = 0.0  # 窗口内成本滚动和（CNY；无 budget tracker 时恒 0）
        _step_findings = 0  # 本步新 finding 数（工具记账循环里累加，步首清零）
        _step_cost = 0.0  # 本步成本（budget.add 后算增量，步首清零）
        _prev_cost = 0.0  # 上一步累计成本（算本步增量用）
        _progress_actions: deque = deque(maxlen=10)  # C1：最近工具动作（进度锚点）
        _last_progress_step = -99

        # ---------- S3 运行时四件套状态 ----------
        from secpt.core.context.estimator import effective_window, est_messages_tokens, est_tokens
        from secpt.core.context.roundpool import build_new_chain, should_rollover

        _base_window = cfg.context_window or MODEL_WINDOWS.get(model, 64_000)
        window = effective_window(_base_window)
        budget = None
        if cfg.budget_cost_cny > 0:
            from secpt.core.context.budget import BudgetTracker

            # 计价按主模型解析（池化/failover 引入后与 deepseek 默认价脱钩；
            # failover 段按主模型价近似——预算上限语义不受影响）
            budget = BudgetTracker(cfg.budget_cost_cny, warn_pct=cfg.budget_warn_pct, model=model)
        if self.summarizer is None:
            from secpt.core.context.summarize import get_summarizer

            self.summarizer = get_summarizer(self.llm, model="deepseek-chat", api_key=api_key)
        summary_k = 0  # 压缩摘要编号
        session_round = cfg.round_offset  # 会话轮次（resume 后接续）
        session_start_t = time.time()
        session_steps = 0

        # 检查点恢复预算/detector/steps/摘要编号（事件流重放的合理近似）
        if _checkpoint:
            steps = int(_checkpoint["steps"])
            summary_k = int(_checkpoint["summary_k"])
            detector.trigger_count = int(_checkpoint["nudges"])
            if budget is not None:
                # tokens 记入累计（后续 add() 按总额重算 cost），warned 同步防重复告警
                budget.prompt_tokens += int(_checkpoint["prompt_tokens"])
                budget.completion_tokens += int(_checkpoint["completion_tokens"])
                budget.cost_cny = float(_checkpoint["cost_cny"])
                if (
                    budget.cost_limit_cny > 0
                    and budget.cost_cny >= budget.cost_limit_cny * budget.warn_pct
                ):
                    budget.warned = True

        log.info(
            "engine.start",
            model=model,
            max_steps=cfg.max_steps,
            max_wall_s=cfg.max_wall_s,
            workspace=str(cfg.work_dir),
            resumed=bool(_checkpoint),
        )
        if self.extra_tools:
            log.info("engine.extra_tools", tools=sorted(self.extra_tools))

        _v = self.verbose
        # 工具执行上下文：插件工具据此定位 ws/（ContextVar 隔离多 worker）
        from secpt.runtime.workspace_ctx import set_work_dir

        _ws_token = set_work_dir(cfg.work_dir)
        try:
            # max_steps/max_wall_s <= 0 表示不限（7×24 长任务）
            while (cfg.max_steps <= 0 or steps < cfg.max_steps) and (
                cfg.max_wall_s <= 0 or (time.time() - t0) < cfg.max_wall_s
            ):
                # 协作式取消（Web 控制台/外部管理）：jobs/<job_id>/cancel 标记文件
                # 存在 → 每步优雅退出（status=cancelled，落 transcript，释放容器）。
                # 检查含父任务链（-iXXXX/-r2/-rs 剥后缀）——取消沿任务树传播，
                # 后续派生的续段/重建引擎同样立即退出。O(1) stat，每步一次可忽略。
                with contextlib.suppress(Exception):
                    from secpt.core.job.resume import jobs_dir as _jd

                    _jid = cfg.job_id
                    _cancelled = False
                    while _jid:
                        if (_jd() / _jid / "cancel").is_file():
                            _cancelled = True
                            break
                        _prev = _jid
                        import re as _re

                        _jid = _re.sub(r"-(?:i[0-9a-fA-F]{4,8}|r\d+|rs)$", "", _jid)
                        if _jid == _prev:
                            break
                    if _cancelled:
                        status = "cancelled"
                        log.info("engine.cancelled", job_id=cfg.job_id, step=steps)
                        print("\n[engine] CANCELLED（协作式取消，优雅退出）", file=sys.stderr)
                        break
                steps += 1
                session_steps += 1
                # ROE 节速（authorized 实战模式）：step 之间 sleep，避免对目标造成突发压力
                if cfg.roe_throttle_ms > 0 and steps > 1:
                    await asyncio.sleep(cfg.roe_throttle_ms / 1000.0)
                elapsed = time.time() - t0
                _step_findings = 0  # 三档止损：本步成果/成本计数（步首清零）
                _step_cost = 0.0

                if self.hooks and hasattr(self.hooks, "on_step"):
                    # 人在环路（steer）：hook 返回字符串则注入为 user 消息
                    # （SteerHook 从 SQLite 队列拉人工操舵提示，下一轮即生效）
                    _steer = self.hooks.on_step(steps, elapsed)
                    if isinstance(_steer, str) and _steer.strip():
                        messages.append({"role": "user", "content": _steer})
                        if hasattr(self.hooks, "on_user_message"):
                            self.hooks.on_user_message(_steer, kind="steer")
                        if _v:
                            print(f"  [steer] {_steer[:80]}...", file=sys.stderr)

                # 每 10 步打进度（structlog：JSON 模式下可按 job_id 检索链路）
                if steps % 10 == 0:
                    log.info(
                        "engine.progress",
                        steps=steps,
                        elapsed_s=round(elapsed, 1),
                        has_new_info=last_tool_has_new_info,
                    )

                if _v:
                    print(
                        f"\n--- step {steps}/{cfg.max_steps}  ({elapsed:.0f}s) ---", file=sys.stderr
                    )

                try:
                    # 发往 LLM 的副本剔除 reasoning_content（它只落盘审计）
                    from secpt.runtime.llm.client import sanitize_messages

                    req_messages = sanitize_messages(messages)
                    if hasattr(self.llm, "call"):
                        resp = await self.llm.call(req_messages, tool_schemas, model)
                    else:
                        # 兼容 harness.py 的函数签名 call_llm(messages, tools, model, api_key)
                        resp = await self.llm(req_messages, tool_schemas, model, api_key)
                except Exception as e:
                    log.warning("engine.llm_error", error=str(e))
                    if model == cfg.model_primary:
                        log.warning("engine.model_fallback", to=cfg.model_fallback)
                        model = cfg.model_fallback
                        continue
                    else:
                        log.error("engine.model_fallback_exhausted")
                        status = "error"
                        break

                msg = resp.choices[0].message
                # 整条 append（含 reasoning_content）
                msg_dict = {"role": "assistant", "content": msg.content or ""}
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    msg_dict["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                if hasattr(msg, "reasoning_content") and msg.reasoning_content:
                    msg_dict["reasoning_content"] = msg.reasoning_content
                messages.append(msg_dict)

                if self.hooks and hasattr(self.hooks, "on_assistant"):
                    self.hooks.on_assistant(msg_dict)

                # 打印正文（思考过程随写随显，不随 verbose 门控）
                if msg.content:
                    preview = msg.content[:300]
                    print(f"  [think] {preview}", file=sys.stderr)

                # ---- S3 预算记账 + 阈值 ----
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    # A 缓存命中可观测：DeepSeek 系 usage 带 cache 命中/未命中 token——
                    # 累计进指标与收尾汇总（append-only 链的 prefix cache 收益从此可量化）
                    _hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
                    _miss = int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0)
                    if _hit or _miss:
                        cache_hit_tokens += _hit
                        cache_miss_tokens += _miss
                        counter_inc("llm_cache_hit_tokens_total", float(_hit))
                        counter_inc("llm_cache_miss_tokens_total", float(_miss))
                else:
                    # fake/无 usage 时回退估算（本轮请求 + 本轮产出）
                    prompt_tokens = est_messages_tokens(req_messages)
                    extra = "".join((tc.function.arguments or "") for tc in (msg.tool_calls or []))
                    completion_tokens = est_tokens((msg.content or "") + extra)
                if budget is not None:
                    budget.add(prompt_tokens, completion_tokens)
                    _step_cost = budget.cost_cny - _prev_cost  # 三档止损：本步成本增量
                    _prev_cost = budget.cost_cny
                if self.hooks and hasattr(self.hooks, "on_tokens"):
                    self.hooks.on_tokens(prompt_tokens, completion_tokens)
                if budget is not None:
                    bst = budget.check()
                    if bst == "exceeded":
                        # 100%：优雅收尾（先落 findings+report.md 再置状态），不抛异常
                        print(
                            f"[engine] budget 100%（{budget.cost_cny:.4f} CNY），优雅收尾",
                            file=sys.stderr,
                        )
                        if self.hooks and hasattr(self.hooks, "on_budget_exceeded"):
                            self.hooks.on_budget_exceeded(messages)
                        status = "budget_exceeded"
                        break
                    if bst == "warn":
                        from secpt.core.context.budget import WARN_80_MSG

                        print("[engine] budget 80%，注入引导消息", file=sys.stderr)
                        messages.append({"role": "user", "content": WARN_80_MSG})
                        if self.hooks and hasattr(self.hooks, "on_user_message"):
                            self.hooks.on_user_message(WARN_80_MSG, kind="budget")
                        if self.hooks and hasattr(self.hooks, "budget_warning"):
                            self.hooks.budget_warning(WARN_80_MSG)

                # 无 tool_calls → 检查 ANSWER:
                if not msg.tool_calls:
                    ans = re.search(r"ANSWER:\s*(\S+)", msg.content or "", re.IGNORECASE)
                    if ans:
                        flag_found = ans.group(1)
                        status = "success"
                        print(f"\n[ANSWER] {flag_found}", file=sys.stderr)
                        with contextlib.suppress(Exception):
                            from secpt.runtime.notify import notify

                            notify(
                                "flag_found",
                                f"secpt 任务 {cfg.job_id} 拿到成果",
                                flag_found[:120],
                            )
                        break
                    print("  [info] no tool_calls, continuing...", file=sys.stderr)
                    continue

                # 执行 tool_calls：并行执行、串行记账。
                # 依赖启发式分组：组内独立调用并发（上限 _PARALLEL_MAX），
                # 组间必须串行（前组 write_file 产物被后组引用等）。
                # 记账（hooks / 发现去重 / messages append / 卡死切换器）仍按原始
                # tool_call 顺序逐一执行，保证 warroom FIFO 配对与事件流顺序
                # 与改造前的串行版完全一致（as_completed 只影响 [out] 打印时机）。
                items = []
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                        _tc_parse_fail_streak = 0  # 成功即清零（自救提示按 episode 计）
                    except json.JSONDecodeError:
                        # PA4.1：不执行、不吞错——回填哨兵，由 _exec_one 返回自救错误串
                        fn_args = _UNPARSEABLE
                        _tc_parse_fail_streak += 1
                    items.append((tc, fn_name, fn_args))

                sem = asyncio.Semaphore(_parallel_max())

                async def _exec_one(item):
                    """执行单个工具调用；失败不拖垮整批（逐项捕获返回错误串）。"""
                    _tc, _name, _args = item
                    if _args is _UNPARSEABLE:
                        return (
                            f"Error: 工具 {_name} 的 arguments 不是合法 JSON，本次未执行。"
                            "修复：① arguments 必须是合法 JSON（注意转义）；"
                            "② 参数复杂时改用 write_file 写脚本文件后 bash 执行；"
                            "③ 单次输出过长可能被截断——缩小规模。"
                        )
                    if _name not in self.tools:
                        return f"Error: unknown tool '{_name}'"
                    try:
                        result = self.tools[_name](_args)
                        if inspect.isawaitable(result):
                            result = await result
                        return result if isinstance(result, str) else str(result)
                    except Exception as e:
                        return f"Error: {e}"

                async def _bounded(item, _sem=sem):
                    async with _sem:
                        return await _exec_one(item)

                for group in _split_parallel_groups(items):
                    # 启动标记（并行前逐条打印，保持原始顺序；流式 UX 过程可见，
                    # 不随 verbose 门控——工具是否在跑要让用户一眼可见）
                    for tc, fn_name, _fn_args in group:
                        arg_preview = tc.function.arguments[:120]
                        print(f"  [tool] {fn_name}  args={arg_preview}", file=sys.stderr)

                    # 组级限速门（组内 bash ≥2 时生效；方案 A 下 bash 单元素组，防御性兜底）：
                    # 被拦的 bash 不启动、直接回填错误串；组内非 bash 照常并发
                    _gate_err = _ratelimit_group_gate(cfg, group)
                    results: list[str] = [""] * len(group)
                    runnable: list[tuple[int, tuple]] = []  # (原始下标, item)
                    for idx, item in enumerate(group):
                        if _gate_err and item[1] == "bash":
                            results[idx] = _gate_err
                            print(f"  [out]  {_gate_err}", file=sys.stderr)
                        else:
                            runnable.append((idx, item))

                    # 并发执行（≤4，Semaphore 限流）；asyncio.wait(FIRST_COMPLETED)
                    # 完成即打印 [out]（完成序 ≠ 启动序）。注意：py3.13 的
                    # as_completed 产出 coroutine 而非 Future，无法回查原始下标，
                    # 故用 wait + tasks[fut] 映射按原始顺序回填 results。
                    tasks = {asyncio.ensure_future(_bounded(item)): idx for idx, item in runnable}
                    pending = set(tasks)
                    while pending:
                        done, pending = await asyncio.wait(
                            pending, return_when=asyncio.FIRST_COMPLETED
                        )
                        for fut in done:
                            idx = tasks[fut]
                            try:
                                out = fut.result()
                            except BaseException as e:  # 含 CancelledError（BaseException 子类）
                                out = f"Error: {e}"
                            results[idx] = out
                            # 完成即打印（完成序 ≠ 启动序；不随 verbose 门控）
                            print(f"  [out]  {out[:300]}", file=sys.stderr)

                    # 串行记账：按原始顺序逐个执行原有回调体（语义与改造前逐一一致）
                    for (tc, fn_name, fn_args), result in zip(group, results, strict=True):
                        counter_inc("tool_calls_total", tool=fn_name)
                        if fn_args is not _UNPARSEABLE:
                            _tool_calls_done += 1  # PA4.2：仅计真实执行的调用
                        # keyguard：PEM 私钥块一律在进入记账/上下文前替换为占位符
                        # （私钥不进 transcript / warroom / 上下文；占位符保留长度信息）
                        result, _n_pk = redact_private_keys(result)
                        if _n_pk:
                            counter_inc("keyguard_redactions_total")
                            if cfg.audit is not None:
                                cfg.audit.log("keyguard.redact", tool=fn_name, blocks=_n_pk)
                            log.warning("engine.keyguard_redact", tool=fn_name, blocks=_n_pk)
                        if self.hooks and hasattr(self.hooks, "on_tool"):
                            self.hooks.on_tool(tc.id, fn_name, fn_args)

                        # 卡死切换器：更新计数器（含⑥完全重复调用判定，按调用指纹）
                        has_new = detector.update(result, call_key=_call_key(fn_name, fn_args))
                        last_tool_has_new_info = has_new
                        if has_new and loop_phase == "nudged":
                            # NUDGED → RUNNING 回退：提醒有效（出现结构性改向），
                            # 不升级收尾（设计 2.1）；wrapping 档不回退（收口由计数做）
                            loop_phase = "running"
                            steps_since_nudge = 0

                        if self.hooks and hasattr(self.hooks, "on_result"):
                            self.hooks.on_result(tc.id, result)
                        elif self.hooks and hasattr(self.hooks, "on_tool_result"):
                            self.hooks.on_tool_result(steps, fn_name, result)

                        # S3：扫描工具输出里的发现（credential/asset），去重后上报
                        for kind, val in _scan_findings(result, flag_re):
                            key = (kind, val)
                            if key in self._finding_seen:
                                continue
                            self._finding_seen.add(key)
                            _step_findings += 1  # 三档止损：成果计数（边际收益窗口用）
                            if self.hooks and hasattr(self.hooks, "on_finding"):
                                self.hooks.on_finding(kind, val, f"step {steps}: {fn_name}")

                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                        # C1：进度锚点记账（5 步一落盘；压缩/轮转后据此重建"做到哪"）
                        _progress_actions.append(
                            (
                                steps,
                                fn_name,
                                " ".join(str(fn_args).split())[:100],
                                " ".join(result.splitlines()[0].split())[:140] if result else "",
                            )
                        )
                        if steps - _last_progress_step >= 5:
                            write_progress_anchor(
                                cfg,
                                steps=steps,
                                session_round=session_round,
                                actions=list(_progress_actions),
                                findings=len(self._finding_seen),
                            )
                            _last_progress_step = steps

                # PA4.1：连续 JSON 解析失败 ≥2 → 自救提示（每个 episode 只发一次；
                # 依附在批结果之后追加 user 消息，不破坏 tool_results 顺序协议）
                if _tc_parse_fail_streak >= 2 and not _tc_rescue_shown:
                    _tc_rescue_shown = True
                    _rescue = _TC_PARSE_RESCUE_MSG.format(n=_tc_parse_fail_streak)
                    messages.append({"role": "user", "content": _rescue})
                    if self.hooks and hasattr(self.hooks, "on_user_message"):
                        self.hooks.on_user_message(_rescue, kind="rescue")
                    counter_inc("loop_rescue_total")
                    log.warning("engine.tc_parse_rescue", streak=_tc_parse_fail_streak)

                # PA4.2：角色分档工具调用硬上限 → 强制收尾（防单角色烧穿预算；
                # 超"限"走既有 wrapping 通道，与 budget_pct 同款收尾指令）
                if (
                    cfg.max_tool_calls > 0
                    and _tool_calls_done >= cfg.max_tool_calls
                    and loop_phase == "running"
                ):
                    loop_phase = "wrapping"
                    wrap_reason = "tool_cap"
                    wrap_steps = 0
                    counter_inc("loop_wrapping_total")
                    _emit_loop_phase(
                        self.hooks, cfg.audit, log, phase="wrapping", reason=wrap_reason
                    )
                    messages.append({"role": "user", "content": WRAP_MSG})
                    if self.hooks and hasattr(self.hooks, "on_user_message"):
                        self.hooks.on_user_message(WRAP_MSG, kind="wrap")
                    log.warning(
                        "engine.tool_call_cap", done=_tool_calls_done, cap=cfg.max_tool_calls
                    )

                # 卡死切换器：必要时注入提醒（user 角色）——PA2：先试导师诊断换路线
                if detector.should_nudge():
                    nudge_msg = detector.nudge()
                    if self._tool_pool:
                        # 卡死时刻正是需要新工具的时刻——池内工具可 request_tools 升级
                        nudge_msg += (
                            "\n另：若卡点与工具面有关（二进制/需常驻会话/需带外验证/"
                            "需结构化扫描），可调用 request_tools 启用备选工具"
                            f"（池内: {sorted(self._tool_pool)}）。"
                        )
                    mentor_advice = None
                    if (
                        self._mentor_enabled
                        and self._mentor_model
                        and mentor_calls < self._mentor_max_calls
                        and not os.environ.get("SECPT_FAKE_LLM")
                    ):
                        mentor_calls += 1
                        mentor_advice = await self._mentor_advice(
                            cfg.objective,
                            list(_progress_actions),
                            nudge_msg,
                            sorted(self._finding_seen),
                        )
                        counter_inc(
                            "mentor_calls_total", status="ok" if mentor_advice else "fallback"
                        )
                        if mentor_advice:
                            nudge_msg = f"【导师建议】{mentor_advice}"
                            log.info(
                                "engine.mentor_advice", call=mentor_calls, chars=len(mentor_advice)
                            )
                    messages.append({"role": "user", "content": nudge_msg})
                    if self.hooks and hasattr(self.hooks, "on_user_message"):
                        self.hooks.on_user_message(nudge_msg, kind="nudge")
                    log.info("engine.nudge", trigger_count=detector.trigger_count)
                    counter_inc("loop_nudged_total")
                    _emit_loop_phase(self.hooks, cfg.audit, log, phase="nudged")
                    if loop_enabled and loop_phase == "running":
                        loop_phase = "nudged"  # NUDGED 档：开始计无改善步数
                        steps_since_nudge = 0

                # ---- 三档止损状态机：档位迁移（设计 §2）----
                if loop_enabled:
                    # 边际收益滚动窗口（2.2）：窗口满 且 成果=0 且 成本超阈值 → 收尾。
                    if len(_margin_win) == _margin_win.maxlen:
                        _of, _oc = _margin_win.popleft()
                        _margin_findings -= _of
                        _margin_cost -= _oc
                    _margin_win.append((_step_findings, _step_cost))
                    _margin_findings += _step_findings
                    _margin_cost += _step_cost

                    if loop_phase == "nudged":
                        # nudge 后无结构性改善达阈值 → 进入收尾（注入 WRAP_MSG）。
                        # 步数地板：低于 max_steps 35%（绝对地板 20 步）不升级——
                        # fuzz 探索期的重复命令不构成"卡死"，早期 wrap 会掐死送分题
                        steps_since_nudge += 1
                        if steps_since_nudge >= _wrap_after_nudge() and steps >= _wrap_min_steps(
                            cfg
                        ):
                            loop_phase = "wrapping"
                            wrap_reason = "nudge_no_improve"
                            wrap_steps = 0
                            counter_inc("loop_wrapping_total")
                            _emit_loop_phase(
                                self.hooks, cfg.audit, log, phase="wrapping", reason=wrap_reason
                            )
                            messages.append({"role": "user", "content": WRAP_MSG})
                            if self.hooks and hasattr(self.hooks, "on_user_message"):
                                self.hooks.on_user_message(WRAP_MSG, kind="wrap")
                    elif loop_phase == "wrapping":
                        # STALLED 收口：收尾期 N 步仍未产 ANSWER → 优雅退出（不抛异常），
                        # 交编排层决定 RESET 或终态（设计 2.1）
                        wrap_steps += 1
                        if wrap_steps >= _stall_after_wrap():
                            status = "stalled"
                            counter_inc("loop_stalled_total")
                            _emit_loop_phase(
                                self.hooks, cfg.audit, log, phase="stalled", reason=wrap_reason
                            )
                            print(
                                f"[engine] wrapping {wrap_steps} 步未收敛，判 stalled"
                                "（交编排层 RESET/终态）",
                                file=sys.stderr,
                            )
                            with contextlib.suppress(Exception):
                                from secpt.runtime.notify import notify

                                notify(
                                    "job_stalled",
                                    f"secpt 任务 {cfg.job_id} 止损停滞（stalled）",
                                    f"收尾 {wrap_steps} 步未产出 ANSWER"
                                    f"（触发：{wrap_reason or 'nudge'}）；"
                                    "编排层将决定 RESET 重建或终态",
                                )
                            break
                    # 预算/步数接近耗尽（wrap_at_pct，设计 2.1 ②：步数或预算达
                    # 该百分比直接收尾）也触发收尾（单次，不重复注入）
                    if loop_phase == "running" and (
                        (
                            budget is not None
                            and budget.cost_limit_cny > 0
                            and budget.cost_cny >= budget.cost_limit_cny * _wrap_at_pct() / 100.0
                        )
                        or (cfg.max_steps > 0 and steps >= cfg.max_steps * _wrap_at_pct() / 100.0)
                    ):
                        loop_phase = "wrapping"
                        wrap_reason = "budget_pct"
                        wrap_steps = 0
                        counter_inc("loop_wrapping_total")
                        _emit_loop_phase(
                            self.hooks, cfg.audit, log, phase="wrapping", reason=wrap_reason
                        )
                        messages.append({"role": "user", "content": WRAP_MSG})
                        if self.hooks and hasattr(self.hooks, "on_user_message"):
                            self.hooks.on_user_message(WRAP_MSG, kind="wrap")
                    # 零产出高成本（no_yield）：窗口满 且 finding=0 且 成本超阈值
                    if (
                        loop_phase == "running"
                        and len(_margin_win) == _margin_win.maxlen
                        and _margin_findings == 0
                        and _margin_cost >= _margin_cost_cny()
                    ):
                        loop_phase = "wrapping"
                        wrap_reason = "no_yield"
                        wrap_steps = 0
                        counter_inc("loop_wrapping_total")
                        _emit_loop_phase(
                            self.hooks, cfg.audit, log, phase="wrapping", reason=wrap_reason
                        )
                        messages.append({"role": "user", "content": WRAP_MSG})
                        if self.hooks and hasattr(self.hooks, "on_user_message"):
                            self.hooks.on_user_message(WRAP_MSG, kind="wrap")

                # S1：本轮结束，落盘 round 快照（会话轮次号，resume 接续用）
                if self.hooks and hasattr(self.hooks, "on_round_end"):
                    self.hooks.on_round_end(session_round, messages)

                # ---- S3 上下文压缩（非阻塞）：删组+占位同步做，摘要后台生成 ----
                # 红线：messages 整条 append；压缩与 rollover 是仅有的删改/重建点。
                # 放在本轮消息全部 append 之后、rollover 之前：同一阈值下压缩优先
                # （删组把 est 拉回阈值下），轮转只在压缩救不回或步数/墙钟到时触发。
                # 主循环只做微秒级操作（删组/插占位/落请求文件），LLM 摘要交给
                # _compaction_worker 后台任务——compaction 不再阻塞下一步 LLM 调用。
                if self._pending_summary_ks:
                    # 已生成摘要回填占位（从盘上 summary 文件读，主循环单线程改写）
                    from secpt.core.context.compaction import swap_summary_placeholders
                    from secpt.core.job.resume import jobs_dir as _jobs_dir

                    _swapped = swap_summary_placeholders(
                        messages, _jobs_dir(), cfg.job_id, self._pending_summary_ks
                    )
                    if _swapped:
                        log.info("engine.summary_backfilled", count=_swapped)

                if window > 0 and summary_k < 999:
                    from secpt.core.context.compaction import (
                        placeholder_content,
                        plan_compaction,
                        write_compaction_request,
                    )
                    from secpt.core.job.resume import jobs_dir as _jobs_dir

                    plan = plan_compaction(messages, window)
                    if plan is not None:
                        remaining, removed_msgs, first_idx = plan
                        k = summary_k
                        summary_k += 1
                        # 落请求文件（file-backed 队列）：后台 worker 拉取生成摘要
                        write_compaction_request(
                            cfg.job_id,
                            _jobs_dir(),
                            k=k,
                            window=window,
                            objective=cfg.objective,
                            removed_msgs=removed_msgs,
                        )
                        # 原位插占位摘要（明细已删，先给模型交代）
                        remaining.insert(
                            min(first_idx, len(remaining)),
                            {"role": "user", "content": placeholder_content(k)},
                        )
                        # C1：压缩后追加结构化进度锚点（append-only；锚点防"失忆"——
                        # 旧消息删除后，模型仍知道做到哪、试过什么、什么被否定）
                        _anchor = progress_anchor_text(cfg)
                        if _anchor and not any(
                            PROGRESS_ANCHOR_MARK in str(m.get("content") or "")
                            for m in remaining[-6:]
                        ):
                            remaining.append({"role": "user", "content": _anchor})
                        messages = remaining
                        self._pending_summary_ks.add(k)
                        counter_inc("compaction_count")
                        log.info("engine.compact_requested", k=k, removed=len(removed_msgs))
                        if self.hooks and hasattr(self.hooks, "on_compaction_requested"):
                            self.hooks.on_compaction_requested(k, len(removed_msgs), window)
                        if summary_k == 5:
                            print(
                                "[engine] 提示：压缩已 5 次仍频繁——若网关模型为 128k 上下文，"
                                "设 SECPT_CONTEXT_WINDOW=128000 可显著减少压缩",
                                file=sys.stderr,
                            )
                        # 懒启动后台摘要 worker（崩溃残留请求也会被它扫到）
                        if self._bg_task is None or self._bg_task.done():
                            self._bg_task = asyncio.create_task(self._compaction_worker(log))

                # ---- S3 会话轮转（rollover 是唯一允许"重建 messages"的地方）----
                do_roll, reason = should_rollover(
                    steps_in_round=session_steps,
                    wall_in_round=time.time() - session_start_t,
                    est_tokens=est_messages_tokens(messages),
                    window=window,
                    round_max_steps=cfg.round_max_steps,
                    round_max_wall_s=cfg.round_max_wall_s,
                )
                if do_roll:
                    handoff = await self.summarizer(cfg.objective, messages)
                    new_messages = build_new_chain(messages[0], handoff)
                    session_round += 1
                    hint = ""
                    if self.hooks and hasattr(self.hooks, "on_rollover"):
                        # watchdog 在 on_rollover 评估空转，返回"换层"提示
                        hint = self.hooks.on_rollover(session_round, new_messages, handoff) or ""
                    if hint:
                        new_messages[1]["content"] += "\n\n" + hint
                    # C1：轮转（同会话换链）同样注入结构化锚点——handoff 是 LLM 摘要，
                    # 锚点是确定性事实，二者互补
                    _anchor = progress_anchor_text(cfg)
                    if _anchor:
                        new_messages.append({"role": "user", "content": _anchor})
                    messages = new_messages
                    session_steps = 0
                    session_start_t = time.time()
                    log.info("engine.rollover", session_round=session_round, reason=reason)
        except Exception as e:
            log.error("engine.fatal", error=str(e))
            status = "error"
        finally:
            with contextlib.suppress(Exception):
                from secpt.runtime.workspace_ctx import reset_work_dir

                reset_work_dir(_ws_token)
            # 收尾：停掉后台摘要 worker（残留 pending 请求由 resume 或
            # `secpt worker summarize` 兜底，摘要文件才是完成真相源）
            if self._bg_task is not None:
                self._bg_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._bg_task
            # F2：释放本 job 的常驻沙箱容器（此前仅 atexit + 4h TTL 兜底——
            # 多意图任务（frontier 每意图一个子 job）逐意图累积容器）。
            # sandbox 未启用时是 no-op；意图内复用语义不变（容器活到本 run 结束）。
            with contextlib.suppress(Exception):
                from secpt.runtime.sandbox import release_container

                release_container(cfg.job_id)

        # ------------------------------------------------------------------
        # 结束
        # ------------------------------------------------------------------
        # 精确退出原因（success/error/budget_exceeded 已在循环内设置）
        if status == "failed" and flag_found is None:
            if cfg.max_steps > 0 and steps >= cfg.max_steps:
                status = "step_exceeded"
            elif cfg.max_wall_s > 0 and (time.time() - t0) >= cfg.max_wall_s:
                status = "wallclock_exceeded"

        wall = time.time() - t0
        cost = budget.cost_cny if budget is not None else 0.0
        print(f"\n{'=' * 50}")
        print(f"总步数: {steps}")
        print(f"墙钟:   {wall:.1f}s")
        print(f"Flag:   {flag_found or '未拿到'}")
        print(f"卡死切换器触发: {detector.trigger_count} 次")
        _cache_total = cache_hit_tokens + cache_miss_tokens
        if _cache_total:
            print(
                f"缓存命中: {cache_hit_tokens}/{_cache_total} prompt tokens"
                f" ({100.0 * cache_hit_tokens / _cache_total:.1f}%)"
            )
        if budget is not None:
            print(
                f"成本:   {cost:.4f} CNY  (prompt={budget.prompt_tokens}, "
                f"completion={budget.completion_tokens})"
            )
        print(f"会话轮次: {session_round}  压缩摘要数: {summary_k}")
        if loop_enabled and loop_phase != "running":
            print(f"止损档位: {loop_phase}（{wrap_reason or 'nudge'}）")

        transcript_path = cfg.work_dir / "transcript.json"
        with open(transcript_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        print(f"Transcript: {transcript_path}  ({len(messages)} messages)")

        if flag_found:
            print("\n[engine] SUCCESS")
        else:
            print("\n[engine] FAILED")

        return RunResult(
            flag=flag_found,
            steps=steps,
            wall_s=wall,
            status=status,
            messages=messages,
            cost_cny=cost,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )


# ---------------------------------------------------------------------------
# delegate 子智能体工具（市场共识对齐：子智能体是"工具"不是"层"）
#
# Cognition《Don't Build Multi-Agents》与 Claude Code/Deep Agents 的共同形态：
# 一次委托 → 独立上下文的一次性子链 → 结论压缩成一条消息返回。要点：
# - 禁递归：子链以 enable_delegate=False 构造（结构性保证，不靠提示词纪律）
# - 串行执行：子链 run() 覆写进程级 SECPT_JOB_ID/WORK_DIR，并行会互踩（见分组器）
# - 治理继承：子链继承父级 scope/ratelimit/audit/sandbox 配置（授权边界不放松）
# - 工作区挂靠：子链 ws 在父 ws 之下（<父ws>/delegate-N/ws），父可 read_file 取其产物
# - 独立小预算：SECPT_DELEGATE_STEPS（默认 25，5-60）/ SECPT_DELEGATE_WALL（默认 600s）
# - v1 边界：子链过程不落父事件流（transcript 在子工作区）；结论即返回值
# ---------------------------------------------------------------------------

DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "派生一次性子智能体（独立上下文）完成一件聚焦的分析/检索任务，只回最终结论。"
            "适合：分析二进制/枚举端点/核对指纹/读大文件找线索等独立读重型工作；"
            "不适合多步利用链（那需要你自己保持上下文连续）。"
            "子智能体看不到你的对话，task 描述必须自包含（目标/已知信息/期望产出）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "自包含的任务描述（子智能体看不到你的对话历史）",
                }
            },
            "required": ["task"],
        },
    },
}


def make_delegate_tool(cfg: RunConfig, llm):
    """构建 delegate 工具 (schema, fn)。fn 为 async，返回子链结论文本（永不抛）。"""
    seq = [0]

    async def _delegate(args: dict) -> str:
        task = str((args or {}).get("task") or "").strip()
        if not task:
            return "Error: task 必填（子智能体看不到你的对话，任务描述必须自包含）"
        seq[0] += 1
        n = seq[0]
        try:
            steps = max(5, min(60, int(os.environ.get("SECPT_DELEGATE_STEPS", "25") or 25)))
        except ValueError:
            steps = 25
        try:
            wall = max(60.0, float(os.environ.get("SECPT_DELEGATE_WALL", "600") or 600))
        except ValueError:
            wall = 600.0
        child_cfg = RunConfig(
            job_id=f"{cfg.job_id}-dl{n}",
            objective=task,
            system_extra="",
            workspace=cfg.work_dir / f"delegate-{n}",  # 挂父 ws 下：父可读子产物
            max_steps=steps,
            max_wall_s=int(wall),
            bash_timeout_s=cfg.bash_timeout_s,
            bash_whitelist=cfg.bash_whitelist,
            model_primary=cfg.model_primary,
            model_fallback=cfg.model_fallback,
            scope=cfg.scope,  # 治理继承：授权边界不放松
            ratelimit=cfg.ratelimit,
            audit=cfg.audit,
            context_window=cfg.context_window,
            sandbox=cfg.sandbox,
            agent_id=f"{cfg.agent_id or 'main'}/delegate",
            round_max_steps=cfg.round_max_steps,
            round_max_wall_s=cfg.round_max_wall_s,
        )
        child_cfg.system_extra = (
            "你是一次性**子智能体**：只完成父智能体委派的一个聚焦任务，不扩大范围。\n"
            f"{build_tools_doc(child_cfg)}\n"
            "完成后用 ANSWER: <结论> 收尾；结论必须自包含（父智能体看不到你的过程）。"
        )
        # 环境变量保护：子链 run() 覆写进程级 SECPT_JOB_ID/WORK_DIR，结束恢复
        _saved = {k: os.environ.get(k) for k in ("SECPT_JOB_ID", "SECPT_WORK_DIR")}
        try:
            child = Engine(child_cfg, llm=llm, enable_delegate=False)  # 禁递归
            result = await asyncio.wait_for(child.run(), timeout=wall)
        except TimeoutError:
            return f"[delegate-{n} 超时 {int(wall)}s] 子任务未按时完成（产物在 delegate-{n}/ws）"
        except Exception as e:
            return f"[delegate-{n} 失败] {e}"
        finally:
            for k, v in _saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        tail = ""
        for m in reversed(result.messages or []):
            c = str(m.get("content") or "")
            if m.get("role") == "assistant" and c.strip():
                tail = c.strip()[:800]
                break
        head = result.flag or f"（无 ANSWER）status={result.status}"
        return (
            f"[delegate-{n} 结论] status={result.status} steps={result.steps} "
            f"ws=delegate-{n}/ws\n{head}\n{tail}"
        )

    return DELEGATE_SCHEMA, _delegate
