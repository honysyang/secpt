"""拦截审批引擎：人在环路的"关键节点人把关"（ARTEX intercept 的可迁移实现）。

模型提出动作，运行时决定能否执行：
- 规则表（SQLite，数据驱动）：name/priority/match_target(tool|command)/
  pattern(regex)/action(allow|deny|ask)/message/timeout_s/timeout_action
- 求值顺序：allow > deny > ask；未命中默认放行（SECPT_INTERCEPT_DEFAULT=ask 可收紧）
- ask：写 pending 记录，阻塞等待 secpt approve/deny，超时按 timeout_action（默认 deny）
- 内置种子只拦"毁灭性"命令（渗透常规动作不打扰，实战节奏优先）；
  种子是普通规则数据，可停用/删除——治理策略不是硬编码黑盒
- 拦截消息标注"平台管控·非目标防御"（ARTEX systemBlockMessage 同款——
  防模型把平台拦截当成目标 WAF 去绕过）

注入点：engine run_bash（白名单校验后、执行前）与插件工具 exec 前。
审批等待用轮询（跨进程：CLI 与 worker 是两个进程），默认 300s 超时。
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def _default_db() -> Path:
    # 调用时解析（模块级常量是 import 期快照：SECPT_VAR 在测试/多任务间漂移，
    # 快照会把审批记录写进别的任务目录——与 JobStore 同款修复口径）
    return Path(os.environ.get("SECPT_VAR", "var")) / "intercept.sqlite"


DEFAULT_DB = _default_db()  # 兼容存量引用；新代码请用 _default_db()

BLOCK_TAG = "【平台管控·非目标防御】"

# D：规则表 TTL 缓存——每次 run_bash 都新建 Interceptor 并全表 SELECT 纯浪费；
# 按库路径缓存 5 秒（跨进程改规则 ≤5s 生效；同进程改规则立即失效）。进程内锁保护。
_RULES_TTL_S = 5.0
_rules_cache: dict[str, tuple[float, list[dict]]] = {}
_rules_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intercept_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    priority INTEGER DEFAULT 5,
    match_target TEXT NOT NULL DEFAULT 'command',   -- tool | command
    pattern TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('allow','deny','ask')),
    message TEXT DEFAULT '',
    timeout_s REAL DEFAULT 300,
    timeout_action TEXT DEFAULT 'deny',
    enabled INTEGER DEFAULT 1,
    builtin INTEGER DEFAULT 0,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS intercept_pending (
    id TEXT PRIMARY KEY,
    job_id TEXT DEFAULT '',
    target TEXT DEFAULT '',          -- 被拦的工具/命令
    preview TEXT DEFAULT '',         -- 命令前 200 字
    rule_id INTEGER,
    rule_name TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',   -- pending | approved | denied | timeout
    decided_by TEXT DEFAULT '',      -- human | llm | timeout
    ts REAL NOT NULL,
    decided_at REAL
);
"""

_SEED_RULES = [
    # (name, priority, target, pattern, action, message, timeout_s, timeout_action)
    (
        "内置-毁灭性命令-询问",
        100,
        "command",
        r"\brm\s+(-[a-zA-Z]*\s+)*(-rf|-fr)\b|\bmkfs\b|\bdd\s+.*of=/dev/\S+|\bshutdown\b|\breboot\b|\bpoweroff\b|\buserdel\b|\bfdisk\b",
        "ask",
        "毁灭性/系统级命令，需人工确认",
        300,
        "deny",
    ),
    (
        "内置-写系统目录-询问",
        90,
        "command",
        r">\s*/(etc|usr|bin|sbin|boot)/|\bchmod\s+(-R\s+)?777\s+/(?:\s|$)",
        "ask",
        "写入系统目录/过度授权，需人工确认",
        300,
        "deny",
    ),
    (
        "内置-网络出境-提示",
        10,
        "command",
        r"\b(nc|netcat|ncat)\s+(-[a-zA-Z]*\s+)*\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",
        "allow",
        "外连命令（已放行；如需收紧改 ask）",
        300,
        "deny",
    ),
]


class Interceptor:
    """规则求值 + 审批等待。eval 永远返回 (allowed: bool, message: str)。"""

    def __init__(self, db_path: Path | str | None = None, *, seed: bool = True) -> None:
        self.db_path = Path(db_path) if db_path else _default_db()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
        if seed:
            self._seed()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _seed(self) -> None:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM intercept_rules").fetchone()["n"]
            if n > 0:
                return
            for name, prio, target, pat, action, msg, to, toa in _SEED_RULES:
                c.execute(
                    """INSERT INTO intercept_rules
                       (name,priority,match_target,pattern,action,message,
                        timeout_s,timeout_action,enabled,builtin,ts)
                       VALUES (?,?,?,?,?,?,?,?,1,1,?)""",
                    (name, prio, target, pat, action, msg, to, toa, time.time()),
                )

    # ---------------- 规则管理 ----------------

    def add_rule(
        self,
        *,
        name: str,
        pattern: str,
        action: str,
        match_target: str = "command",
        priority: int = 5,
        message: str = "",
        timeout_s: float = 300.0,
        timeout_action: str = "deny",
    ) -> int:
        if action not in ("allow", "deny", "ask"):
            raise ValueError("action 必须是 allow/deny/ask")
        re.compile(pattern)  # 预校验正则
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO intercept_rules
                   (name,priority,match_target,pattern,action,message,
                    timeout_s,timeout_action,enabled,builtin,ts)
                   VALUES (?,?,?,?,?,?,?,?,1,0,?)""",
                (
                    name,
                    priority,
                    match_target,
                    pattern,
                    action,
                    message,
                    timeout_s,
                    timeout_action,
                    time.time(),
                ),
            )
            self._invalidate_cache()  # D：同进程改规则立即生效（跨进程靠 TTL）
            return int(cur.lastrowid)

    def list_rules(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM intercept_rules ORDER BY priority DESC, id").fetchall()
        return [dict(r) for r in rows]

    def remove_rule(self, rule_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM intercept_rules WHERE id=? AND builtin=0", (rule_id,))
            self._invalidate_cache()
            return cur.rowcount > 0

    def set_enabled(self, rule_id: int, enabled: bool) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE intercept_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id)
            )
            self._invalidate_cache()
            return cur.rowcount > 0

    # ---------------- 求值与审批 ----------------

    def _cached_rules(self) -> list[dict]:
        """启用的规则列表（D：按库路径 TTL 缓存；缺省 5s，跨进程改规则 ≤5s 生效）。"""
        key = str(self.db_path)
        now = time.time()
        with _rules_lock:
            hit = _rules_cache.get(key)
            if hit is not None and now - hit[0] < _RULES_TTL_S:
                return hit[1]
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM intercept_rules WHERE enabled=1 ORDER BY priority DESC, id"
            ).fetchall()
        rules = [dict(r) for r in rows]
        with _rules_lock:
            _rules_cache[key] = (now, rules)
        return rules

    def _invalidate_cache(self) -> None:
        with _rules_lock:
            _rules_cache.pop(str(self.db_path), None)

    def _match(self, *, tool: str, command: str) -> list[dict]:
        hits = []
        for r in self._cached_rules():
            hay = tool if r["match_target"] == "tool" else command
            try:
                if re.search(r["pattern"], hay or ""):
                    hits.append(r)
            except re.error:
                continue
        return hits

    @staticmethod
    def _pick(hits: list[dict]) -> dict | None:
        """allow > deny > ask；同级取高 priority（查询已排序）。"""
        for action in ("allow", "deny", "ask"):
            for h in hits:
                if h["action"] == action:
                    return h
        return None

    def decide(self, pending_id: str, approved: bool, *, by: str = "human") -> bool:
        with self._conn() as c:
            cur = c.execute(
                """UPDATE intercept_pending SET status=?, decided_by=?, decided_at=?
                   WHERE id=? AND status='pending'""",
                ("approved" if approved else "denied", by, time.time(), pending_id),
            )
            return cur.rowcount > 0

    def pending_list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM intercept_pending WHERE status='pending' ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def wait_decision(
        self, pending_id: str, *, timeout_s: float, poll_s: float = 1.0
    ) -> tuple[bool, str]:
        """阻塞等待人工决定；超时按规则 timeout_action。返回 (allowed, note)。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._conn() as c:
                row = c.execute(
                    "SELECT status FROM intercept_pending WHERE id=?", (pending_id,)
                ).fetchone()
            if row is None:
                return False, "审批记录不存在"
            if row["status"] == "approved":
                return True, "人工放行"
            if row["status"] == "denied":
                return False, "人工拒绝"
            time.sleep(poll_s)
        self.decide(pending_id, False, by="timeout")
        return False, "审批超时"

    def eval(
        self, *, tool: str, command: str, job_id: str = "", wait: bool = True
    ) -> tuple[bool, str]:
        """对一次工具调用求值。返回 (allowed, message)。

        - allow 或未命中 → (True, "")
        - deny → (False, 拦截消息)
        - ask → 写 pending，人工审批（wait=True 时阻塞；False 时直接按超时动作拒绝）
        """
        hits = self._match(tool=tool, command=command)
        rule = self._pick(hits)
        if rule is None or rule["action"] == "allow":
            return True, ""
        if rule["action"] == "deny":
            return False, f"{BLOCK_TAG} 命中规则[{rule['name']}]：{rule['message'] or 'deny'}"
        # ask
        pid = f"icp-{uuid.uuid4().hex[:10]}"
        with self._conn() as c:
            c.execute(
                """INSERT INTO intercept_pending
                   (id,job_id,target,preview,rule_id,rule_name,status,ts)
                   VALUES (?,?,?,?,?,?, 'pending', ?)""",
                (pid, job_id, tool, (command or "")[:200], rule["id"], rule["name"], time.time()),
            )
        print(
            f"[intercept] ⏸ 等待审批 #{pid} 规则[{rule['name']}]："
            f"{(command or '')[:80]}（secpt approve {pid} / deny）"
        )
        with contextlib.suppress(Exception):
            from secpt.runtime.notify import notify

            notify(
                "approval_pending",
                f"secpt 待审批 #{pid}（规则：{rule['name']}）",
                f"{(command or '')[:200]}\n批准：secpt approve {pid} / 拒绝：secpt deny {pid}",
            )
        if not wait:
            self.decide(pid, False, by="no-wait")
            return False, f"{BLOCK_TAG} 规则[{rule['name']}]要求审批（非阻塞模式拒绝）"
        allowed, note = self.wait_decision(pid, timeout_s=float(rule["timeout_s"]))
        if allowed:
            return True, ""
        return False, (
            f"{BLOCK_TAG} 规则[{rule['name']}]：{note}。"
            f"如需执行请调整命令或停用该规则（secpt intercept）"
        )
