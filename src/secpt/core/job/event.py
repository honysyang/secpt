"""S1 事件流：EventSink（jsonl 主写 + SQLite 双写，SQLite 失败不阻断）。

同时实现 Engine 需要的 hooks 回调接口：
    on_step / on_assistant / on_tool / on_result / on_round_end
    on_tokens / on_finding / on_compaction(_requested/_completed) / on_rollover
    on_budget_exceeded

事件类型固定，不许随意扩展（新增须配运行时读路径与测试）：
    assistant / tool_call / tool_result / finding / round_end
    / budget_warning / rollover / status_change / error / compaction
    / routed / step / user_msg / verdict
    / tokens / compaction_requested / compaction_completed（非阻塞 compaction）
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl  # POSIX：跨进程 append 文件锁（Windows 不可用则降级为纯线程锁）
except ImportError:  # pragma: no cover - Windows 分支
    fcntl = None

from secpt.core.job.evlock import PEV_LOCK
from secpt.core.job.store import JobStore


def _flock_wait(fd: int, timeout_s: float = 2.0) -> bool:
    """限时排他锁：拿到锁返回 True（调用方负责解锁）；超时返回 False。

    R3 加固：emit 在事件循环里被同步调用，阻塞式 flock 是少数能把整个循环
    冻死且零日志的等待点（持锁进程僵死时尤甚）。O_APPEND 下单行 write 内核
    原子，flock 只为 seq 尾读同步服务——超时降级无锁直写，代价至多 seq 跳号，
    绝不能冻结主循环。默认 2 秒（10 秒会在僵死锁上每步拖住事件循环）。
    """
    if fcntl is None:
        return True
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


EVENT_TYPES = {
    "assistant",
    "tool_call",
    "tool_result",
    "finding",
    "round_end",
    "budget_warning",
    "rollover",
    "status_change",
    "error",
    "compaction",
    "routed",
    "step",  # 步进心跳（watch 实时视图的分界）
    "user_msg",  # 注入的 user 消息（人工操舵/换层提醒/预算警告/收尾指令）
    "verdict",  # evaluation 层终裁结果（AI 评判，见 secpt/evaluation/）
    "tokens",  # 每步 LLM token 记账（checkpoint 恢复预算的数据源）
    "compaction_requested",  # 主循环已删组+插占位（非阻塞 compaction 的请求入队）
    "compaction_completed",  # 后台摘要完成、<k>.summary.md 已落盘
    "loop_phase",  # 三档止损状态机档位迁移（nudged/wrapping/stalled，见 runtime/engine.py）
    "plan_event",  # v4 编排：Commander/规划轮结果（信念结算/决策/派单数）
    "intent_event",  # v4 编排：意图生命周期（领取/结算），任务池流转的轨迹层
}

# 事件 payload 结构目录（结构化加固轮）：类型 → 必备 payload 键。
# 校验口径 = "缺键告警不阻断"（每类型一次 stderr 告警）——事件流是观测层，
# 不许因结构瑕疵打断主链路；目录本身是**单点事实源**：新增事件类型必须同时
# 登记 EVENT_TYPES 与 EVENT_CATALOG（有测试保证目录完备）。
EVENT_CATALOG: dict[str, frozenset[str]] = {
    "assistant": frozenset({"content", "tool_calls"}),
    "tool_call": frozenset({"call_id", "tool", "args"}),
    "tool_result": frozenset({"call_id", "status", "bytes", "preview"}),
    "finding": frozenset({"id", "kind", "value"}),
    # round 归事件行信封管（frontier 父流聚合的合成 round_end 只带 n_messages）
    "round_end": frozenset({"n_messages"}),
    "budget_warning": frozenset({"message"}),
    "rollover": frozenset({"round", "n_messages", "handoff"}),
    "status_change": frozenset({"status"}),
    "error": frozenset({"message"}),
    "compaction": frozenset({"k", "removed_messages", "window", "summary"}),
    "compaction_requested": frozenset({"k", "removed_messages", "window"}),
    "compaction_completed": frozenset({"k", "removed_messages", "window", "summary"}),
    "routed": frozenset({"profile", "via"}),
    "step": frozenset({"n", "elapsed_s"}),
    "user_msg": frozenset({"kind", "content"}),
    "tokens": frozenset({"prompt", "completion", "cost_cny"}),
    # verdict 由 evaluation 层直接 append（不经 write()），目录条目为存量文档
    "verdict": frozenset({"complete"}),
    # 三档止损状态机（docs/设计-工作循环形态.md §2）：phase ∈ nudged/wrapping/stalled
    "loop_phase": frozenset({"phase"}),
    # 编排层事件（frontier 直写父流，非 EventSink 通道；catalog 登记保契约一致）
    "plan_event": frozenset({"decision"}),
    "intent_event": frozenset({"iid", "title", "status"}),
}


def _classify_output(out: str) -> str:
    """根据工具输出判定 status ∈ ok|empty|error。"""
    if out is None:
        return "empty"
    out = str(out)
    if not out.strip():
        return "empty"
    if out.startswith("Error:"):
        return "error"
    return "ok"


class EventSink:
    """写 var/jobs/<job_id>/events.jsonl（append-only）+ 转存 SQLite。"""

    def __init__(
        self,
        job_id: str,
        jobs_dir: Path,
        store: JobStore | None = None,
        *,
        agent_id: str = "",
        chain_id: str = "",
    ) -> None:
        self.job_id = job_id
        self.jobs_dir = Path(jobs_dir)
        self.job_dir = self.jobs_dir / job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.job_dir / "events.jsonl"
        self.store = store
        self.agent_id = agent_id  # 事件行 trace 字段（非空才写入，缺省保持旧行格式）
        self.chain_id = chain_id
        # 进程内写锁：specialist 多链 to_thread 并行共享同一 sink 写同一文件，
        # _seq 自增与 append 必须原子（PR-A 并发安全）
        self._lock = threading.Lock()
        self._schema_warned: set[str] = set()  # payload 缺键告警去重（每类型一次）
        # 初始 seq = 已有行数（jsonl 用行号做 seq）
        self._seq = self._count_lines()

    # ---------------- 底层写入 ----------------

    def _count_lines(self) -> int:
        if not self.events_path.exists():
            return 0
        # 硬杀/断电可能留下无换行的半行：先补一个换行，避免新事件粘在半行后
        # 永久损坏（读取方永远解析失败）；errors=replace 防半个 UTF-8 字符崩初始化。
        try:
            with open(self.events_path, "rb+") as bf:
                bf.seek(0, os.SEEK_END)
                if bf.tell() > 0:
                    bf.seek(-1, os.SEEK_END)
                    if bf.read(1) != b"\n":
                        bf.seek(0, os.SEEK_END)
                        bf.write(b"\n")
        except OSError:
            pass
        with open(self.events_path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)

    def _tail_seq(self) -> int:
        """读文件末尾 8KB 解析最后一行 seq，保证跨进程并发时 seq 单调不回退。

        用独立 "rb" 句柄：append 模式的 fh 不可读（read 会抛 UnsupportedOperation）。
        """
        try:
            with open(self.events_path, "rb") as rf:
                rf.seek(0, 2)
                size = rf.tell()
                rf.seek(max(0, size - 8192))
                tail = rf.read().decode("utf-8", errors="replace")
            for line in reversed(tail.splitlines()):
                if not line.strip():
                    continue
                try:
                    return int(json.loads(line).get("seq", 0))
                except (json.JSONDecodeError, ValueError, AttributeError):
                    return 0
            return 0
        except OSError:
            return 0

    def write(self, etype: str, payload: dict, round_: int = 0) -> int:
        if etype not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {etype!r}")
        # 结构化校验（缺键告警每类型一次，不阻断——事件流是观测层）
        required = EVENT_CATALOG.get(etype)
        if required:
            missing = required - set(payload or {})
            if missing and etype not in self._schema_warned:
                self._schema_warned.add(etype)
                print(
                    f"[sink] 事件 {etype!r} payload 缺键 {sorted(missing)}"
                    f"（EVENT_CATALOG 契约，已记录不阻断）",
                    file=__import__("sys").stderr,
                )
        with self._lock, PEV_LOCK:
            # 跨写入方（编排 _parent_event / Web 注入）可能在本实例初始化后
            # 追加过事件：持全局锁重读尾 seq，保证任何交错下 seq 单调不回退
            self._seq = max(self._seq, self._tail_seq())
            self._seq += 1
            line = {
                "seq": self._seq,
                "job_id": self.job_id,
                "ts": __import__("time").time(),
                "round": round_,
                "type": etype,
                "payload": payload,
            }
            if self.agent_id:
                line["agent_id"] = self.agent_id
            if self.chain_id:
                line["chain_id"] = self.chain_id
            data = json.dumps(line, ensure_ascii=False) + "\n"
            # 主写：jsonl append（断电最多丢一步）。POSIX 下叠 flock 排他锁：
            # 本进程与其他进程（secpt worker summarize 等）写同一文件时单行不交错
            with open(self.events_path, "a", encoding="utf-8") as f:
                if fcntl is not None:
                    _lock_timeout = float(os.environ.get("SECPT_EVENT_FLOCK_TIMEOUT_S", "2") or 2)
                    locked = _flock_wait(f.fileno(), timeout_s=_lock_timeout)
                    if not locked:
                        print(
                            f"[sink] flock 超时（{_lock_timeout:.0f}s），降级无锁写入",
                            file=sys.stderr,
                        )
                    try:
                        # 其他进程可能已追加：尾读同步 seq，保证全局单调
                        tail = self._tail_seq()
                        if tail >= self._seq:
                            self._seq = tail + 1
                            line["seq"] = self._seq
                            data = json.dumps(line, ensure_ascii=False) + "\n"
                        f.write(data)
                        # 必须在解锁前 flush：Python 文本写默认块缓冲，若先 LOCK_UN
                        # 再 close，下一进程的 tail 读不到这一行 → 分配重复 seq。
                        f.flush()
                    finally:
                        if locked:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                else:
                    f.write(data)
                    f.flush()
            # 次写：SQLite（失败不阻断）
            if self.store is not None:
                try:
                    self.store.add_event(self.job_id, etype, payload, round_)
                except Exception as e:
                    print(f"[sink] sqlite event failed (ignored): {e}")
            return self._seq

    # ---------------- Engine hooks ----------------

    def on_user_message(self, content: str, kind: str = "info") -> None:
        """注入类 user 消息落事件流（steer=人工操舵 / nudge=换层提醒 / budget=预算警告）。"""
        self.write("user_msg", {"kind": kind, "content": content[:800]})

    def on_step(self, idx: int, elapsed_s: float) -> None:
        # 步进心跳：watch 实时视图的分界（一行，低成本）
        self.write("step", {"n": idx, "elapsed_s": round(elapsed_s, 1)})

    def on_assistant(self, msg_dict: dict) -> None:
        self.write(
            "assistant",
            {
                "content": (msg_dict.get("content") or "")[:2000],
                "reasoning": (msg_dict.get("reasoning_content") or "")[:800],
                "tool_calls": [
                    {
                        "id": tc.get("id", ""),
                        "name": (tc.get("function") or {}).get("name", ""),
                        "arguments": ((tc.get("function") or {}).get("arguments", "") or "")[:500],
                    }
                    for tc in msg_dict.get("tool_calls") or []
                ],
            },
        )

    def on_tool(self, call_id: str, name: str, args: dict) -> None:
        """PR-A：带 call_id 落 tool_call 事件（与 on_result 精确配对，不再依赖 FIFO）。"""
        self.write(
            "tool_call",
            {
                "call_id": call_id,
                "tool": name,
                "args": json.dumps(args, ensure_ascii=False)[:500],
            },
        )

    def on_result(self, call_id: str, out: str) -> None:
        self.write(
            "tool_result",
            {
                "call_id": call_id,
                "status": _classify_output(out),
                "bytes": len(str(out or "")),
                "preview": str(out or "")[:500],
            },
        )

    def on_round_end(self, round_: int, messages: list[dict]) -> None:
        # 落盘 rounds/NNN.messages.json（resume 的 checkpoint 来源）
        try:
            from secpt.core.job.rounds import save_round

            save_round(self.job_id, self.jobs_dir, round_, messages)
        except Exception as e:
            print(f"[sink] save_round failed (ignored): {e}")
        self.write(
            "round_end",
            {
                "round": round_,
                "n_messages": len(messages),
            },
            round_=round_,
        )

    def status_change(self, status: str, *, round_: int = 0, error: str = "") -> None:
        payload = {"status": status}
        if error:
            payload["error"] = error[:500]
        self.write("status_change", payload, round_=round_)

    def finding(self, fid: str, kind: str, value: str, evidence: str = "") -> None:
        self.write(
            "finding",
            {
                "id": fid,
                "kind": kind,
                "value": value[:500],
                "evidence": evidence[:500],
            },
        )

    def budget_warning(self, msg: str) -> None:
        self.write("budget_warning", {"message": msg[:500]})

    def error(self, msg: str, round_: int = 0) -> None:
        self.write("error", {"message": msg[:500]}, round_=round_)

    # ---------------- S3 hooks ----------------

    def on_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """每步 LLM 调用后记账：tokens 事件落流（恢复预算的真相源）+ cost 记账。"""
        cost = 0.0
        try:
            from secpt.core.context.budget import tokens_cost

            cost = tokens_cost(prompt_tokens, completion_tokens)
        except Exception as e:
            print(f"[sink] tokens_cost failed (ignored): {e}")
        try:
            self.write(
                "tokens",
                {
                    "prompt": int(prompt_tokens),
                    "completion": int(completion_tokens),
                    "cost_cny": round(cost, 6),
                },
            )
        except Exception as e:
            print(f"[sink] tokens event failed (ignored): {e}")
        if self.store is None:
            return
        try:
            rec = self.store.get_job(self.job_id)
            if rec is not None:
                rec.cost_cny = round(rec.cost_cny + cost, 6)
                self.store.upsert_job(rec)
        except Exception as e:
            print(f"[sink] on_tokens failed (ignored): {e}")

    def on_finding(self, kind: str, value: str, evidence: str = "") -> None:
        """Engine 报告新发现（kind ∈ flag|credential|asset|...）。"""
        fid = f"{kind}-{self._seq + 1}"
        self.finding(fid, kind, value, evidence)
        # 转存 findings 表（S3：report.md / 验证器 / watchdog 的持久化来源）
        if self.store is not None:
            try:
                self.store.add_finding(self.job_id, fid, kind, value, evidence)
            except Exception as e:
                print(f"[sink] add_finding failed (ignored): {e}")

    def on_compaction(self, k: int, summary: str, removed: int, window: int) -> None:
        """记录一次 Compaction 完成（S3 验收要求的 legacy 类型，watch 兼容）。"""
        self.write(
            "compaction",
            {
                "k": k,
                "removed_messages": removed,
                "window": window,
                "summary": summary[:2000],
            },
        )

    def on_compaction_requested(self, k: int, removed: int, window: int) -> None:
        """非阻塞 compaction：主循环已删组+插占位，摘要请求入队（pending 队列）。"""
        self.write(
            "compaction_requested",
            {
                "k": k,
                "removed_messages": removed,
                "window": window,
            },
        )

    def on_compaction_completed(self, k: int, summary: str, removed: int, window: int) -> None:
        """非阻塞 compaction：后台摘要已生成并落盘 <k>.summary.md。"""
        self.write(
            "compaction_completed",
            {
                "k": k,
                "removed_messages": removed,
                "window": window,
                "summary": summary[:2000],
            },
        )

    def on_rollover(self, round_: int, messages: list[dict], handoff: str = "") -> None:
        """记录一次 Rollover：落 round 快照（新链）+ rollover 事件。"""
        try:
            from secpt.core.job.rounds import save_round

            save_round(
                self.job_id, self.jobs_dir, round_, messages, handoff={"handoff": handoff[:2000]}
            )
        except Exception as e:
            print(f"[sink] save_round(rollover) failed (ignored): {e}")
        self.write(
            "rollover",
            {
                "round": round_,
                "n_messages": len(messages),
                "handoff": handoff[:2000],
            },
            round_=round_,
        )

    def on_budget_exceeded(self, messages: list[dict]) -> None:
        """预算 100% 优雅收尾：先落 findings + 阶段性 report.md，再置状态。"""
        report = self.job_dir / "report.md"
        try:
            findings = self.store.list_findings(self.job_id) if self.store else []
            lines = [
                f"# {self.job_id}",
                "",
                "状态：budget_exceeded（预算耗尽，优雅收尾）",
                f"最后消息数：{len(messages)}",
                "",
                "## 已确认发现",
            ]
            if findings:
                for f in findings:
                    ev = f"（{f['evidence']}）" if f.get("evidence") else ""
                    lines.append(f"- [{f['kind']}] {f['value']}{ev}")
            else:
                lines.append("（暂无已落盘的 finding）")
            report.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            print(f"[sink] report.md failed (ignored): {e}")
        self.status_change("budget_exceeded")
