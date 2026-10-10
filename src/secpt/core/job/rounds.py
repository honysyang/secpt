"""S1 round 存档：messages 快照到 var/jobs/<job_id>/rounds/NNN.messages.json。

resume 流程：load_latest_round 拿到最后一轮的完整 messages 作为 Engine 初始链。
落盘前 sanitize：剔除最后一组"不完整"的 assistant+tool_calls（有 tool_calls 但
缺对应 tool message 会触发 API 400）。本阶段只存档不压缩（压缩是 S3 的事）。
"""

from __future__ import annotations

import json
from pathlib import Path

ROUND_FMT = "{:03d}.messages.json"


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """去掉尾部不完整的 assistant+tool_calls 组。

    规则：从尾部向前找到最后一个 assistant；若它带 tool_calls，则它之后必须
    有同数量的 tool 消息。不足则把该 assistant 与其后所有 tool 消息一起剔除，
    继续向前检查（保证链里没有孤立 tool_call）。
    """
    msgs = list(messages)
    while True:
        # 找最后一个 role == assistant
        last_assistant = None
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                last_assistant = i
                break
        if last_assistant is None:
            break
        assistant = msgs[last_assistant]
        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            break
        # 统计该 assistant 之后 tool 消息（属于它的 call_id）
        needed = {tc.get("id") for tc in tool_calls}
        have = 0
        tail = msgs[last_assistant + 1:]
        for m in tail:
            if m.get("role") == "tool" and m.get("tool_call_id") in needed:
                have += 1
        if have >= len(needed):
            break  # 完整，无需裁剪
        # 不完整：剔除该 assistant 及其后的所有消息
        msgs = msgs[:last_assistant]
    return msgs


def save_round(job_id: str, jobs_dir: Path, n: int, messages: list[dict],
               handoff: dict | None = None) -> Path:
    """落盘 round n 的 messages 快照。返回文件路径。"""
    rounds_dir = Path(jobs_dir) / job_id / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    path = rounds_dir / ROUND_FMT.format(n)
    payload = {
        "round": n,
        "handoff": handoff,
        "messages": sanitize_messages(messages),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # 原子替换，防止读到半截文件
    return path


def load_latest_round(job_id: str, jobs_dir: Path) -> tuple[int, list[dict]]:
    """读取最新 round。不存在或空则返回 (0, [])。"""
    rounds_dir = Path(jobs_dir) / job_id / "rounds"
    if not rounds_dir.is_dir():
        return 0, []
    files = sorted(rounds_dir.glob("[0-9][0-9][0-9].messages.json"))
    if not files:
        return 0, []
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    msgs = data.get("messages") or []
    return int(data.get("round", 0)), sanitize_messages(msgs)


def load_round(job_id: str, jobs_dir: Path, n: int) -> list[dict] | None:
    """读取指定 round 的 messages。不存在返回 None。"""
    path = Path(jobs_dir) / job_id / "rounds" / ROUND_FMT.format(n)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return sanitize_messages(data.get("messages") or [])
