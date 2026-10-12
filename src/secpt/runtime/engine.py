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
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from secpt.runtime.stuck import StuckDetector

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

# shell 元字符：白名单命令默认以 argv 数组执行（禁 shell=True），出现这些字符说明
# 模型期望 shell 解释器——只有 profile 显式放行 bash/sh 时才允许（此时以 bash -c 执行）
_SHELL_META = ("|", ";", "&", ">", "<", "$", "`", "\\", "*")
_SHELL_EXES = ("bash", "sh")


def _has_unquoted_shell_meta(command: str) -> bool:
    """shell 元字符仅统计引号外的：引号内的 ; | $ * 等是参数内容，argv 直传执行安全
    （如 python3 -c 'a;b'、sed 's|x|y|'、find -name "*.py"）。"""
    quote: str | None = None
    for i, ch in enumerate(command):
        if quote:
            if ch == quote and command[i - 1] != "\\":
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in _SHELL_META:
            return True
    return False


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
    initial_messages: list[dict] | None = None  # S1 resume：非空则直接作为初始链（含首条 system）
    round_offset: int = 0  # S1 resume：续跑后 round 编号继续，避免覆盖旧 checkpoint
    ratelimit: object | None = None  # S2 全局限速器（.check(command)->list[str]，非空拦截）
    context_window: int | None = None  # S3 上下文窗口（token）；None=按模型查表
    sandbox: str = "auto"  # bash 执行环境：auto（有 docker 用容器）/ docker（强制）/ none

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


def build_tools_doc(cfg: RunConfig) -> str:
    return (
        f"- bash(command)  白名单命令：{', '.join(sorted(cfg.bash_whitelist))}，"
        f"超时{cfg.bash_timeout_s}s。command 必须是字符串（不支持数组），"
        "内部按空格拆成参数直接执行（不经 shell）："
        "不支持管道 |、重定向 >、分号 ;、变量 $、通配符 *、&&/||。"
        "引号内的元字符只是参数内容、可以直接用；确需组合逻辑时，"
        "先用 write_file 写 Python/Shell 脚本到工作目录，再 bash 执行该脚本。"
        "工作目录：{cfg.work_dir}，所有相对路径都自动落到它下面\n"
        f"- read_file(path)  读文件（相对路径=工作目录下），{READ_MAX // 1024}KB上限\n"
        "- list_dir(path)  列目录（相对路径=工作目录下），深度1\n"
        f"- write_file(path, content)  写文件到 {cfg.work_dir}/ 下"
        "（相对路径自动落到工作目录，禁止越界；可写 python 脚本再 bash 执行）"
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
        if exe not in cfg.bash_whitelist:
            return (
                f"Error: '{exe}' 不在白名单。可用命令：{sorted(cfg.bash_whitelist)}。"
                f"bash 的工作目录已经是你的工作目录，不要用 cd；"
                f"遍历文件用 find 或 ls -R；复杂逻辑用 write_file 写脚本再执行。"
            )
        uses_shell = exe in _SHELL_EXES
        if not uses_shell and _has_unquoted_shell_meta(command):
            return (
                "Error: 命令含未引用的 shell 元字符（| ; & > < $ ` * 等），"
                "但当前不允许 shell 解释。引号内的元字符是参数内容、可以直接用；"
                "确需管道/循环时，用 write_file 写脚本到工作目录，再 bash 执行该脚本文件。"
            )
        argv: list[str] = [exe, "-c", command] if uses_shell else parts

        # 人在环路-审批：白名单/元字符之后、执行之前（SECPT_INTERCEPT=0 可全局关）
        if os.environ.get("SECPT_INTERCEPT", "1") != "0":
            try:
                from secpt.governance.intercept import Interceptor

                ok, why = Interceptor().eval(tool="bash", command=command, job_id=cfg.job_id)
                if not ok:
                    return why
            except Exception as e:
                print(f"[intercept] 求值失败（放行）: {e}", file=sys.stderr)

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
                return f"Error: sandbox 不可用: {e}"

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

    async def run(self) -> RunResult:
        cfg = self.cfg
        cfg.work_dir.mkdir(parents=True, exist_ok=True)

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")

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
        detector = StuckDetector(cfg.stuck_threshold, cfg.stuck_increment)

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

        _v = self.verbose
        print(
            f"[engine] job={cfg.job_id} model={model}  max_steps={cfg.max_steps}  "
            f"timeout={cfg.max_wall_s}s",
            file=sys.stderr,
        )
        print(f"[engine] workspace: {cfg.work_dir}", file=sys.stderr)
        if self.extra_tools:
            print(f"[engine] 插件工具: {sorted(self.extra_tools)}", file=sys.stderr)

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

                # 每 10 步打印进度行（常驻，不进 verbose 门控）
                if steps % 10 == 0:
                    print(
                        f"[progress] 已用 {steps} 步 / 已过 {elapsed:.0f}s / 最近工具"
                        f"{'有' if last_tool_has_new_info else '无'}新信息",
                        file=sys.stderr,
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
                    print(f"[engine] LLM error: {e}", file=sys.stderr)
                    if model == cfg.model_primary:
                        print(f"[engine] fallback to {cfg.model_fallback}", file=sys.stderr)
                        model = cfg.model_fallback
                        continue
                    else:
                        print("[engine] fallback also failed", file=sys.stderr)
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

                # 打印正文
                if msg.content:
                    preview = msg.content[:300]
                if _v and msg.content:
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

                # 执行 tool_calls
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        fn_args = {}
                    arg_preview = tc.function.arguments[:120]
                    if _v:
                        print(f"  [tool] {fn_name}  args={arg_preview}", file=sys.stderr)

                    if self.hooks and hasattr(self.hooks, "on_tool"):
                        self.hooks.on_tool(fn_name, fn_args)

                    if fn_name not in self.tools:
                        result = f"Error: unknown tool '{fn_name}'"
                    else:
                        try:
                            result = self.tools[fn_name](fn_args)
                            if inspect.isawaitable(result):
                                result = await result
                        except Exception as e:
                            result = f"Error: {e}"

                    # 截断打印
                    if _v:
                        print(f"  [out]  {result[:300]}", file=sys.stderr)

                    # 卡死切换器：更新计数器
                    has_new = detector.update(result)
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
                    print(f"  [nudge] {nudge_msg[:80]}...", file=sys.stderr)

                # S1：本轮结束，落盘 round 快照（会话轮次号，resume 接续用）
                if self.hooks and hasattr(self.hooks, "on_round_end"):
                    self.hooks.on_round_end(session_round, messages)

                # ---- S3 上下文压缩：token 超阈值 → 删最老整组 + 原位插六段摘要 ----
                # 红线：messages 整条 append；压缩与 rollover 是仅有的删改/重建点。
                # 放在本轮消息全部 append 之后、rollover 之前：同一阈值下压缩优先
                # （删组把 est 拉回阈值下），轮转只在压缩救不回或步数/墙钟到时触发。
                if window > 0 and summary_k < 999:
                    from secpt.core.context.compaction import compact_messages

                    messages, removed_n, summary = await compact_messages(
                        messages, cfg.objective, self.summarizer, window, k=summary_k
                    )
                    if removed_n:
                        summary_k += 1
                        print(f"[engine] compact #{summary_k}: 删 {removed_n} 条", file=sys.stderr)
                        if summary_k == 5:
                            print(
                                "[engine] 提示：压缩已 5 次仍频繁——若网关模型为 128k 上下文，"
                                "设 SECPT_CONTEXT_WINDOW=128000 可显著减少压缩",
                                file=sys.stderr,
                            )
                        if self.hooks and hasattr(self.hooks, "on_compaction"):
                            self.hooks.on_compaction(summary_k - 1, summary, removed_n, window)

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
                    print(
                        f"[engine] rollover → session {session_round}（{reason}）", file=sys.stderr
                    )
        except Exception as e:
            print(f"[engine] fatal error: {e}", file=sys.stderr)
            status = "error"

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
