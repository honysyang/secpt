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
    status: str = ""       # ok | empty | error（tool_result 分类）
    preview: str = ""      # 工具输出预览（events 里截断 500 字符）


@dataclass
class Evidence:
    job_id: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    answer: str | None = None          # agent 自报的 ANSWER（无则 None）
    answer_in_outputs: bool = False    # ANSWER 值是否出现在任一工具输出中
    n_assistant_msgs: int = 0

    def transcript_excerpt(self, max_chars: int = 4000) -> str:
        """给 judge 的紧凑轨迹摘要。"""
        lines: list[str] = []
        for tc in self.tool_calls[-60:]:
            arg = tc.args[:120]
            lines.append(f"[{tc.status or '?'}] {tc.name}({arg}) -> {tc.preview[:200]}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[-max_chars:]
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
    for cand in (Path(job_dir) / "ws" / "transcript.json",
                 Path(job_dir) / "ws" / "ws" / "transcript.json"):
        if cand.is_file():
            return cand
    return None


def _extract_answer(events: list[dict], transcript: Path | None) -> str | None:
    """agent 自报 ANSWER：优先完整 transcript，其次 events 里的 assistant 内容。"""
    contents: list[str] = []
    if transcript is not None:
        try:
            messages = json.loads(transcript.read_text(encoding="utf-8"))
            for m in messages:
                if m.get("role") == "assistant" and m.get("content"):
                    contents.append(str(m["content"]))
        except Exception:  # noqa: BLE001
            pass
    for ev in events:
        if ev.get("type") == "assistant":
            content = (ev.get("payload") or {}).get("content") or ""
            if content:
                contents.append(content)
    for content in reversed(contents):
        m = ANSWER_RE.search(content)
        if m:
            return m.group(1).strip()
    return None


def collect_evidence(job_id: str, jobs_dir: Path) -> Evidence:
    """汇总一个 job 的评判证据。"""
    job_dir = Path(jobs_dir) / job_id
    events = load_events(job_dir)
    transcript = _find_transcript(job_dir)

    by_id: dict[str, ToolCall] = {}
    order: list[str] = []
    findings: list[dict] = []
    pending_seq = 0               # 单调计数器（不能用 len(order)，pop 后会重复）
    for ev in events:
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        if etype == "tool_call":
            tc = ToolCall(call_id="", name=payload.get("tool", ""),
                          args=payload.get("args", ""))
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
        elif etype == "finding":
            findings.append(payload)

    answer = _extract_answer(events, transcript)

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
            except Exception:  # noqa: BLE001
                pass
        if not answer_in_outputs:
            for tc in by_id.values():
                if needle in tc.preview:
                    answer_in_outputs = True
                    break

    return Evidence(
        job_id=job_id,
        tool_calls=list(by_id.values()),
        findings=findings,
        answer=answer,
        answer_in_outputs=answer_in_outputs,
        n_assistant_msgs=sum(1 for ev in events if ev.get("type") == "assistant"),
    )
