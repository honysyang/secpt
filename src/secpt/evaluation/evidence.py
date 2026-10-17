"""证据收集：从 job 目录提取评判所需的轨迹摘要。

来源：
- events.jsonl（append-only 事件流：assistant / tool_call / tool_result / finding）
- ws/transcript.json（Engine 完整 messages 终态，含未截断的工具输出）

注意：不依赖任何 flag 格式正则。ANSWER: 是 agent 自报协议（取第一行非空内容），
评判时它只作为"待核实的声明"，而非完成判据本身。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ANSWER_RE = re.compile(r"ANSWER:\s*(.+)", re.IGNORECASE)


@dataclass
class ToolCall:
    call_id: str
    name: str
    args: str
    status: str = ""  # ok | empty | error（tool_result 分类）
    preview: str = ""  # 工具输出预览（events 里截断 500 字符）
    sub: str = ""  # 子 job 来源（frontier 聚合流 payload.subjob；单链恒 ""）


@dataclass
class Evidence:
    job_id: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    answer: str | None = None  # agent 自报的 ANSWER（无则 None）
    answer_sub: str = ""  # 产出 ANSWER 的子 job（聚合流防跨会话串味；单链 ""）
    answer_in_outputs: bool = False  # ANSWER 值是否出现在任一工具输出中
    n_assistant_msgs: int = 0

    def transcript_excerpt(self, max_chars: int = 4000) -> str:
        """给 judge 的紧凑轨迹摘要：头 + 尾双窗口采样。

        单一尾部窗口会让多 rollover 的长任务丢失前段攻击路径成型过程（利用
        链怎么搭起来的对"是否真正完成"的判定至关重要）。头窗给前 20 条调用
        （侦察/路径成型），尾窗给最近 40 条（收尾证据），中间以省略标记断开。
        """
        head_n, tail_n = 20, 40
        calls = self.tool_calls
        if len(calls) <= head_n + tail_n + 5:
            window = calls
            omitted = 0
        else:
            window = calls[:head_n] + calls[-tail_n:]
            omitted = len(calls) - head_n - tail_n
        lines: list[str] = []
        for i, tc in enumerate(window):
            if omitted and i == head_n:
                lines.append(f"...（中间省略 {omitted} 条调用）...")
            arg = tc.args[:120]
            lines.append(f"[{tc.status or '?'}] {tc.name}({arg}) -> {tc.preview[:200]}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars // 2] + "\n...(截断)...\n" + text[-max_chars // 2 :]
        return text


def load_events(job_dir: Path) -> list[dict]:
    path = Path(job_dir) / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def _find_transcript(job_dir: Path) -> Path | None:
    for cand in (
        Path(job_dir) / "ws" / "transcript.json",
        Path(job_dir) / "ws" / "ws" / "transcript.json",
    ):
        if cand.is_file():
            return cand
    return None


def _extract_answer(events: list[dict], transcript: Path | None) -> tuple[str | None, str]:
    """agent 自报 ANSWER：优先完整 transcript，其次 events 里的 assistant 内容。

    返回 (answer, subjob)：subjob 为产出 ANSWER 的子 job（frontier 聚合流的
    payload.subjob 标记；transcript 路径与单链恒 ""）。
    """
    contents: list[tuple[str, str]] = []
    if transcript is not None:
        try:
            messages = json.loads(transcript.read_text(encoding="utf-8"))
            for m in messages:
                if m.get("role") == "assistant" and m.get("content"):
                    contents.append((str(m["content"]), ""))
        except Exception:
            pass
    for ev in events:
        if ev.get("type") == "assistant":
            payload = ev.get("payload") or {}
            content = payload.get("content") or ""
            if content:
                contents.append((str(content), str(payload.get("subjob") or "")))
    for content, sub in reversed(contents):
        m = ANSWER_RE.search(content)
        if m:
            return m.group(1).strip(), sub
    return None, ""


def collect_evidence(job_id: str, jobs_dir: Path) -> Evidence:
    """汇总一个 job 的评判证据。"""
    job_dir = Path(jobs_dir) / job_id
    events = load_events(job_dir)
    transcript = _find_transcript(job_dir)

    by_id: dict[str, ToolCall] = {}
    order: list[str] = []
    findings: list[dict] = []
    pending_seq = 0  # 单调计数器（不能用 len(order)，pop 后会重复）
    for ev in events:
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        if etype == "tool_call":
            tc = ToolCall(
                call_id="",
                name=payload.get("tool", ""),
                args=payload.get("args", ""),
                sub=str(payload.get("subjob") or ""),
            )
            # 与后续 tool_result 配对：engine 的 on_result 顺序与 on_tool 一致，
            # 按出现顺序 FIFO 回填
            key = f"pending-{pending_seq}"
            pending_seq += 1
            by_id[key] = tc
            order.append(key)
        elif etype == "tool_result":
            if order:
                key = order.pop(0)
                tc = by_id[key]
                tc.call_id = payload.get("call_id", "")
                tc.status = payload.get("status", "")
                tc.preview = payload.get("preview", "")
                if not tc.sub:
                    tc.sub = str(payload.get("subjob") or "")
        elif etype == "finding":
            findings.append(payload)

    answer, answer_sub = _extract_answer(events, transcript)

    # answer_in_outputs：优先完整 transcript 的 tool 消息，退化为 events 预览
    answer_in_outputs = False
    if answer:
        needle = answer
        if transcript is not None:
            try:
                messages = json.loads(transcript.read_text(encoding="utf-8"))
                for m in messages:
                    if m.get("role") == "tool" and needle in str(m.get("content") or ""):
                        answer_in_outputs = True
                        break
            except Exception:
                pass
        if not answer_in_outputs:
            for tc in by_id.values():
                if needle in tc.preview:
                    answer_in_outputs = True
                    break
        if not answer_in_outputs and len(needle) >= 8:
            # 长任务尾部链丢失证据（33 次 rollover 后 transcript 只剩结尾）——
            # 从全量事件库回溯（tsec-run-1214：58 个 correct=true 在 events 里，
            # 尾部 76 条消息却没有，judge 误判 INCOMPLETE）
            try:
                from secpt.core.job.store import JobStore

                hits = JobStore().search_traces(needle, limit=3)
                if hits:
                    answer_in_outputs = True
            except Exception:
                pass

    return Evidence(
        job_id=job_id,
        tool_calls=list(by_id.values()),
        findings=findings,
        answer=answer,
        answer_sub=answer_sub,
        answer_in_outputs=answer_in_outputs,
        n_assistant_msgs=sum(1 for ev in events if ev.get("type") == "assistant"),
    )
