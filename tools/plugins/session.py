"""session 插件：命名有状态会话（pty 伪终端）。

解决"一次一调用" bash 的工具瓶颈：su/ssh/mysql/交互式 shell 这类多步握手
程序需要持久会话。session_open 起一个 pty 支撑的后台进程，session_send 写入
并可选 expect 等待，输出按游标增量返回。

治理边界（与 bash 工具同口径）：
- open 的初始命令走 argv 数组（禁 shell=True），首 token 必须在
  DEFAULT_BASH_WHITELIST ∪ SECPT_BASH_EXTRA（逗号分隔 basename）内
- 初始命令过 scope 校验（build_scope_validator + extract_scope_targets，
  loopback 豁免与 engine 一致）；会话建立后的输入是程序交互，不再逐条过
  scope（等价于 bash 的管道/脚本内目标——argv 首跳已把守）
- 每进程最多 8 个会话，单会话空闲 1800s 自动回收，输出截断 6000 字符

profile 装配：tools: [session] 声明即装配（数据驱动，无新执行器）。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import time

SCHEMA = {
    "type": "object",
    "description": (
        "命名有状态会话（pty）。open 建立持久进程（初始命令须在白名单内），"
        "send 写入数据（可选 expect 等待响应），read 取增量输出，close 关闭，"
        "list 列出活跃会话。"
    ),
    "properties": {
        "action": {
            "type": "string",
            "enum": ["open", "send", "read", "close", "list"],
            "description": "会话操作",
        },
        "name": {"type": "string", "description": "会话名（open/send/read/close 用）"},
        "command": {
            "type": "string",
            "description": "open：初始命令（argv 文本，首 token 白名单）",
        },
        "data": {"type": "string", "description": "send：写入会话的原文（不会自动补换行）"},
        "expect": {"type": "string", "description": "send：可选——等待输出中出现该串再返回"},
        "timeout_s": {"type": "integer", "description": "send 的 expect 等待上限（默认 10s）"},
    },
    "required": ["action"],
}

_OUT_CAP = 6000
_MAX_SESSIONS = 8
_IDLE_KILL_S = 1800.0

# name -> {proc, master_fd, buf(bytearray), cursor(int), touched(float), reader(Thread)}
_SESSIONS: dict[str, dict] = {}


def _allowed(exe: str) -> bool:
    from secpt.runtime.engine import DEFAULT_BASH_WHITELIST

    extra = {e.strip() for e in os.environ.get("SECPT_BASH_EXTRA", "").split(",") if e.strip()}
    return exe in (DEFAULT_BASH_WHITELIST | extra)


def _scope_ok(command: str) -> str | None:
    """返回 None=放行，否则拒绝原因。与 engine._scope_check 同语义（简化版）。"""
    if os.environ.get("SECPT_SCOPE", "1") == "0":
        return None
    try:
        from secpt.governance.scope import build_scope_validator
        from secpt.runtime.engine import extract_scope_targets, is_loopback_target

        scope = build_scope_validator(None)
        if scope is None:
            return None
        for t in extract_scope_targets(command):
            if is_loopback_target(t):
                continue
            try:
                scope.validate(t)
            except Exception as e:
                if getattr(e, "scope_denied", False):
                    return f"目标 {t} 不在授权范围（scope）"
                return None  # 校验器自身异常 → fail-open（engine 同口径）
    except Exception:
        return None
    return None


def _open(name: str, command: str) -> str:
    if name in _SESSIONS:
        return f"Error: 会话 {name!r} 已存在（先 close 或换个名字）"
    if len(_SESSIONS) >= _MAX_SESSIONS:
        return f"Error: 会话数已达上限 {_MAX_SESSIONS}，先 close 闲置会话"
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"Error: 命令解析失败: {e}"
    if not argv:
        return "Error: 空命令"
    exe = os.path.basename(argv[0])
    if not _allowed(exe):
        return f"Error: '{exe}' 不在会话白名单（可用 SECPT_BASH_EXTRA 扩展）"
    why = _scope_ok(command)
    if why:
        return f"Error: {why}"

    import pty
    import subprocess
    import threading

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(  # noqa: S603（argv 数组，无 shell）
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as e:
        os.close(master_fd)
        os.close(slave_fd)
        return f"Error: 会话启动失败: {e}"
    os.close(slave_fd)
    sess = {
        "proc": proc,
        "master_fd": master_fd,
        "buf": bytearray(),
        "cursor": 0,
        "touched": time.time(),
    }

    def _reader() -> None:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break  # 进程退出/主端关闭
            if not chunk:
                break
            sess["buf"].extend(chunk)

    threading.Thread(target=_reader, daemon=True, name=f"session-{name}").start()
    _SESSIONS[name] = sess
    return f"会话 {name} 已建立（pid {proc.pid}，命令 {exe}）"


def _reap_idle() -> None:
    now = time.time()
    for name in [n for n, s in _SESSIONS.items() if now - s["touched"] > _IDLE_KILL_S]:
        _close(name)


def _close(name: str) -> str:
    sess = _SESSIONS.pop(name, None)
    if sess is None:
        return f"Error: 会话 {name!r} 不存在"
    proc = sess["proc"]
    if proc.poll() is None:
        with _suppress_all():
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=3)
    with _suppress_all():
        os.close(sess["master_fd"])
    return f"会话 {name} 已关闭"


class _suppress_all:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


def _take(sess: dict) -> str:
    """取游标后的增量输出并推进游标。"""
    buf = sess["buf"]
    out = bytes(buf[sess["cursor"] :])
    sess["cursor"] = len(buf)
    if len(buf) > 200_000:  # 缓冲上限：截半保留尾部，防长会话内存膨胀
        del buf[:100_000]
        sess["cursor"] = len(buf)
    return out.decode(errors="replace")[-_OUT_CAP:] or "(无新输出)"


async def _session(args: dict) -> str:
    action = str(args.get("action") or "")
    name = str(args.get("name") or "")
    _reap_idle()

    if action == "list":
        if not _SESSIONS:
            return "（无活跃会话）"
        lines = []
        for n, s in _SESSIONS.items():
            alive = s["proc"].poll() is None
            lines.append(
                f"- {n}: pid={s['proc'].pid} {'活跃' if alive else '已退出'}"
                f" buf={len(s['buf'])}B idle={int(time.time() - s['touched'])}s"
            )
        return "\n".join(lines)

    if not name:
        return "Error: 该操作需要 name 参数"
    if action == "open":
        command = str(args.get("command") or "")
        if not command:
            return "Error: open 需要 command 参数"
        return _open(name, command)
    if action == "close":
        return _close(name)

    sess = _SESSIONS.get(name)
    if sess is None:
        return f"Error: 会话 {name!r} 不存在（用 list 查看，或先 open）"
    if sess["proc"].poll() is not None:
        out = _take(sess)
        return f"会话 {name} 的进程已退出（code {sess['proc'].returncode}）。残余输出：\n{out}"

    if action == "send":
        data = str(args.get("data") or "")
        if not data:
            return "Error: send 需要 data 参数"
        expect = str(args.get("expect") or "")
        timeout_s = max(1, min(60, int(args.get("timeout_s") or 10)))
        os.write(sess["master_fd"], data.encode())
        sess["touched"] = time.time()
        if expect:
            deadline = time.time() + timeout_s

            def _wait() -> str:
                while time.time() < deadline:
                    if expect.encode() in bytes(sess["buf"][sess["cursor"] :]):
                        break
                    if sess["proc"].poll() is not None:
                        break
                    time.sleep(0.1)
                return _take(sess)

            return await asyncio.to_thread(_wait)
        return _take(sess)
    if action == "read":
        sess["touched"] = time.time()
        return _take(sess)
    return f"Error: 未知 action {action!r}（open/send/read/close/list）"


def register() -> dict:
    return {"session": (SCHEMA, _session)}
