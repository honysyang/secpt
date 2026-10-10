"""S1 事件流：EventSink（jsonl 主写 + SQLite 双写，SQLite 失败不阻断）。

同时实现 Engine 需要的 hooks 回调接口：
    on_step / on_assistant / on_tool / on_result / on_round_end
    on_tokens / on_finding / on_compaction / on_rollover / on_budget_exceeded

事件类型固定，不许扩展：
    assistant / tool_call / tool_result / finding / round_end
    / budget_warning / rollover / status_change / error / compaction
（compaction 为 S3 验收要求的事件，S3 spec 覆盖了 S1 的 9 种固定集合）
"""

from __future__ import annotations

import json
from pathlib import Path

from secpt.core.job.store import JobStore

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
    "verdict",  # evaluation 层终裁结果（AI 评判，见 secpt/evaluation/）
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

    def __init__(self, job_id: str, jobs_dir: Path, store: JobStore | None = None) -> None:
        self.job_id = job_id
        self.jobs_dir = Path(jobs_dir)
        self.job_dir = self.jobs_dir / job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.job_dir / "events.jsonl"
        self.store = store
        # 初始 seq = 已有行数（jsonl 用行号做 seq）
        self._seq = self._count_lines()

    # ---------------- 底层写入 ----------------

    def _count_lines(self) -> int:
        if not self.events_path.exists():
            return 0
        with open(self.events_path, encoding="utf-8") as f:
            return sum(1 for _ in f)

    def write(self, etype: str, payload: dict, round_: int = 0) -> int:
        if etype not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {etype!r}")
        self._seq += 1
        line = {
            "seq": self._seq,
            "job_id": self.job_id,
            "ts": __import__("time").time(),
            "round": round_,
            "type": etype,
            "payload": payload,
        }
        # 主写：jsonl append（断电最多丢一步）
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        # 次写：SQLite（失败不阻断）
        if self.store is not None:
            try:
                self.store.add_event(self.job_id, etype, payload, round_)
            except Exception as e:
                print(f"[sink] sqlite event failed (ignored): {e}")
        return self._seq

    # ---------------- Engine hooks ----------------

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

    def on_tool(self, name: str, args: dict) -> None:
        self.write(
            "tool_call",
            {
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
        """每步 LLM 调用后记账：按 config 单价更新 jobs.cost_cny（失败不阻断）。"""
        if self.store is None:
            return
        try:
            from secpt.core.context.budget import tokens_cost

            cost = tokens_cost(prompt_tokens, completion_tokens)
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
        """记录一次 Compaction（S3 验收要求事件类型）。"""
        self.write(
            "compaction",
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
