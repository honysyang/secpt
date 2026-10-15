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
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import structlog

from secpt.runtime.logsetup import configure_logging
from secpt.runtime.metrics import counter_inc
from secpt.runtime.stuck import StuckDetector

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
READ_MAX = 200 * 1024

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

# 同轮独立工具调用并发上限（防单轮输出 token 截断 / 沙箱过载）
_PARALLEL_MAX = 4

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
        if fn_name == "bash":
            # bash 一律串行：先冲掉无副作用组，自身独占一组（不并行、不参与依赖追踪）
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
    # | budget_exceeded | failed | error
    messages: list[dict] = field(default_factory=list)
    cost_cny: float = 0.0  # S3 累计成本（CNY，由 BudgetTracker 记）


# G1 scope：命令里的目标提取与回环判定（纯函数，便于单测）

_BARE_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
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
            return f"Error: {e}（目标必须在授权范围内；如需扩展设置 SECPT_SCOPE_ALLOW）"
        # 校验器自身异常：fail-open + audit（观测治理层不阻断主链路）
        print(f"[scope] 求值失败（放行）: {e}", file=sys.stderr)
        if cfg.audit is not None:
            with contextlib.suppress(Exception):
                cfg.audit.log("scope.error", error=str(e)[:200], command=command[:200])
        return None


def build_tools_doc(cfg: RunConfig) -> str:
    return (
        f"- bash(command)  白名单命令：{', '.join(sorted(cfg.bash_whitelist))}，"
        f"超时{cfg.bash_timeout_s}s。command 必须是字符串（不支持数组），"
        "内部按空格拆成参数直接执行（不经 shell）："
        "不支持未引用的管道 |、重定向 >、分号 ;、后台/串联 &、命令替换 $( ) 与反引号。"
        "引号内的元字符只是参数内容、可以直接用；裸 $VAR 与通配符 * 会按字面传给命令"
        "（argv 直传不展开）。确需组合逻辑/变量展开时，"
        "先用 write_file 写 Python/Shell 脚本到工作目录，再 bash 执行该脚本。"
        "宿主机绝对路径（如 /home/kali/...）在沙箱容器内不可见，一律用相对路径。"
        "工作目录：{cfg.work_dir}，所有相对路径都自动落到它下面\n"
        f"- read_file(path)  读文件（相对路径=工作目录下），{READ_MAX // 1024}KB上限\n"
        "- list_dir(path)  列目录（相对路径=工作目录下），深度1\n"
        f"- write_file(path, content)  写文件到 {cfg.work_dir}/ 下"
        "（相对路径自动落到工作目录，禁止越界；可写 python 脚本再 bash 执行）\n"
        "- 目标授权：网络命令的目标（URL/IP/域名）必须在授权范围内，"
        "范围外会被拒绝执行；本地回环（127.0.0.1/localhost）不受限。"
        "如需访问额外目标，请告知操作者设置 SECPT_SCOPE_ALLOW（逗号分隔 IP/CIDR/域名）\n"
        f"- 并行：同轮可发起最多 {_PARALLEL_MAX} 个相互独立的工具调用"
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
                "description": f"读取文件，{READ_MAX // 1024}KB上限",
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
        return text[:BASH_MAX_OUT] + (
            f"\n...(输出 {len(text)} 字符过长已截断，"
            f"完整内容已存 outputs/{fname}，可 read_file 查看)"
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
        # 错误提示与真实原因（shell 组合构造）不符，模型反复重试烧步数
        uses_shell = exe in _SHELL_EXES
        if not uses_shell and _has_unquoted_shell_chain(command):
            return (
                "Error: 命令含未引用的 shell 组合构造（| ; & > < 或 $( ) / 反引号），"
                "当前按参数直传执行、不经 shell 解释。引号内的元字符只是参数内容、"
                "可以直接用；裸 $VAR 与通配符 * 会按字面传给命令（argv 不展开）。\n"
                "改写示例：① 分号串联 → 拆成多条独立 bash 调用（'a; b' 改为先跑 a 再跑 b）；"
                "② 管道/重定向/循环/变量展开 → 用 write_file 写脚本（如 run.sh）再 bash 执行该脚本文件。"
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

        # 人在环路-审批：白名单/元字符之后、执行之前（SECPT_INTERCEPT=0 可全局关）
        if os.environ.get("SECPT_INTERCEPT", "1") != "0":
            try:
                from secpt.governance.intercept import Interceptor

                ok, why = Interceptor().eval(tool="bash", command=command, job_id=cfg.job_id)
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

    def read_file(path: str) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = work_dir / p  # 与 bash cwd / write_file 一致
        try:
            sz = p.stat().st_size
            if sz > READ_MAX:
                return f"Error: file too large ({sz} > {READ_MAX})"
            return p.read_text(errors="replace")[:BASH_MAX_OUT]
        except Exception as e:
            return f"Error: {e}"

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
        "read_file": lambda a: read_file(a["path"]),
        "list_dir": lambda a: list_dir(a["path"]),
        "write_file": lambda a: write_file(a["path"], a["content"]),
    }


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
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.tools = tools if tools is not None else make_tool_fns(cfg)
        self.extra_tools = extra_tools or {}
        self.tools.update({name: fn for name, (schema, fn) in self.extra_tools.items()})
        self.hooks = hooks
        self.summarizer = summarizer
        self.verbose = verbose
        # 引擎侧 finding 去重（避免同一条 URL/凭据刷屏事件流）
        self._finding_seen: set[tuple[str, str]] = set()
        # 非阻塞 compaction 状态：待回填占位摘要的 k 集合 + 后台摘要 worker 任务
        self._pending_summary_ks: set[int] = set()
        self._bg_task: asyncio.Task | None = None

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

        tool_schemas = build_tool_schemas(cfg) + [
            schema for schema, _fn in self.extra_tools.values()
        ]
        flag_re = cfg.flag_re

        model = cfg.model_primary
        steps = 0
        t0 = time.time()
        flag_found = None
        status = "failed"  # 默认值：循环条件不满足且未显式设置时兜底
        last_tool_has_new_info = True
        detector = StuckDetector(
            cfg.stuck_threshold, cfg.stuck_increment, wait_cap=cfg.stuck_wait_cap
        )

        # ---------- S3 运行时四件套状态 ----------
        from secpt.core.context.estimator import effective_window, est_messages_tokens, est_tokens
        from secpt.core.context.roundpool import build_new_chain, should_rollover

        _base_window = cfg.context_window or MODEL_WINDOWS.get(model, 64_000)
        window = effective_window(_base_window)
        budget = None
        if cfg.budget_cost_cny > 0:
            from secpt.core.context.budget import BudgetTracker

            budget = BudgetTracker(cfg.budget_cost_cny, warn_pct=cfg.budget_warn_pct)
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
        try:
            # max_steps/max_wall_s <= 0 表示不限（7×24 长任务）
            while (cfg.max_steps <= 0 or steps < cfg.max_steps) and (
                cfg.max_wall_s <= 0 or (time.time() - t0) < cfg.max_wall_s
            ):
                steps += 1
                session_steps += 1
                elapsed = time.time() - t0

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
                else:
                    # fake/无 usage 时回退估算（本轮请求 + 本轮产出）
                    prompt_tokens = est_messages_tokens(req_messages)
                    extra = "".join((tc.function.arguments or "") for tc in (msg.tool_calls or []))
                    completion_tokens = est_tokens((msg.content or "") + extra)
                if budget is not None:
                    budget.add(prompt_tokens, completion_tokens)
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
                    except json.JSONDecodeError:
                        fn_args = {}
                    items.append((tc, fn_name, fn_args))

                sem = asyncio.Semaphore(_PARALLEL_MAX)

                async def _exec_one(item):
                    """执行单个工具调用；失败不拖垮整批（逐项捕获返回错误串）。"""
                    _tc, _name, _args = item
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
                        if self.hooks and hasattr(self.hooks, "on_tool"):
                            self.hooks.on_tool(tc.id, fn_name, fn_args)

                        # 卡死切换器：更新计数器（含⑥完全重复调用判定，按调用指纹）
                        has_new = detector.update(result, call_key=_call_key(fn_name, fn_args))
                        last_tool_has_new_info = has_new

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
                            if self.hooks and hasattr(self.hooks, "on_finding"):
                                self.hooks.on_finding(kind, val, f"step {steps}: {fn_name}")

                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

                # 卡死切换器：必要时注入提醒（user 角色）
                if detector.should_nudge():
                    nudge_msg = detector.nudge()
                    messages.append({"role": "user", "content": nudge_msg})
                    if self.hooks and hasattr(self.hooks, "on_user_message"):
                        self.hooks.on_user_message(nudge_msg, kind="nudge")
                    log.info("engine.nudge", trigger_count=detector.trigger_count)

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
                    messages = new_messages
                    session_steps = 0
                    session_start_t = time.time()
                    log.info("engine.rollover", session_round=session_round, reason=reason)
        except Exception as e:
            log.error("engine.fatal", error=str(e))
            status = "error"
        finally:
            # 收尾：停掉后台摘要 worker（残留 pending 请求由 resume 或
            # `secpt worker summarize` 兜底，摘要文件才是完成真相源）
            if self._bg_task is not None:
                self._bg_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._bg_task

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
        if budget is not None:
            print(
                f"成本:   {cost:.4f} CNY  (prompt={budget.prompt_tokens}, "
                f"completion={budget.completion_tokens})"
            )
        print(f"会话轮次: {session_round}  压缩摘要数: {summary_k}")

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
        )
