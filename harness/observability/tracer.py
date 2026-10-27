"""可观测性：执行链路追踪。

记录每次执行的：
- LLM 调用次数和 token 消耗
- 工具调用记录
- 执行时间和步骤
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Span:
    """执行跨度（一次操作）。"""

    name: str
    kind: str  # llm_call | tool_call | step
    start_time: float
    end_time: float = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        """持续时间（毫秒）。"""
        if self.end_time == 0:
            return 0
        return (self.end_time - self.start_time) * 1000


@dataclass
class Trace:
    """执行链路（一次完整任务）。"""

    trace_id: str
    goal: str
    start_time: float
    end_time: float = 0
    spans: list[Span] = field(default_factory=list)
    total_tokens: int = 0
    steps: int = 0
    status: str = "running"  # running | completed | failed

    @property
    def duration_ms(self) -> float:
        """总持续时间（毫秒）。"""
        if self.end_time == 0:
            return 0
        return (self.end_time - self.start_time) * 1000


class Tracer:
    """执行链路追踪器。"""

    def __init__(self, storage_path: str | Path | None = None) -> None:
        self.storage_path = Path(storage_path) if storage_path else None
        self._traces: dict[str, Trace] = {}
        self._current_trace: Trace | None = None
        self._current_span: Span | None = None

    def start_trace(self, goal: str) -> str:
        """开始追踪。

        Args:
            goal: 任务目标

        Returns:
            trace_id
        """
        trace_id = str(uuid.uuid4())[:8]
        self._current_trace = Trace(
            trace_id=trace_id,
            goal=goal,
            start_time=time.time(),
        )
        self._traces[trace_id] = self._current_trace
        return trace_id

    def end_trace(self, status: str = "completed") -> None:
        """结束追踪。"""
        if self._current_trace:
            self._current_trace.end_time = time.time()
            self._current_trace.status = status
            self._save_trace(self._current_trace)
            self._current_trace = None

    def start_span(self, name: str, kind: str = "step", **attributes: Any) -> None:
        """开始一个跨度。"""
        if self._current_trace is None:
            return

        self._current_span = Span(
            name=name,
            kind=kind,
            start_time=time.time(),
            attributes=attributes,
        )

    def end_span(self, error: str | None = None, **attributes: Any) -> None:
        """结束当前跨度。"""
        if self._current_span and self._current_trace:
            self._current_span.end_time = time.time()
            self._current_span.error = error
            self._current_span.attributes.update(attributes)
            self._current_trace.spans.append(self._current_span)

            # 统计
            if self._current_span.kind == "llm_call":
                self._current_trace.total_tokens += self._current_span.attributes.get("tokens", 0)
            elif self._current_span.kind == "tool_call":
                self._current_trace.steps += 1

            self._current_span = None

    def record_llm_call(self, tokens: int, model: str = "") -> None:
        """记录 LLM 调用。"""
        if self._current_trace:
            self._current_trace.total_tokens += tokens

    def record_tool_call(self, tool_name: str, success: bool = True) -> None:
        """记录工具调用。"""
        if self._current_trace:
            self._current_trace.steps += 1

    def get_trace(self, trace_id: str) -> Trace | None:
        """获取追踪记录。"""
        return self._traces.get(trace_id)

    def get_all_traces(self) -> list[Trace]:
        """获取所有追踪记录。"""
        return list(self._traces.values())

    def get_summary(self) -> dict[str, Any]:
        """获取统计摘要。"""
        traces = self.get_all_traces()
        return {
            "total_traces": len(traces),
            "total_tokens": sum(t.total_tokens for t in traces),
            "total_steps": sum(t.steps for t in traces),
            "avg_duration_ms": sum(t.duration_ms for t in traces) / max(len(traces), 1),
            "completed": sum(1 for t in traces if t.status == "completed"),
            "failed": sum(1 for t in traces if t.status == "failed"),
        }

    def _save_trace(self, trace: Trace) -> None:
        """保存追踪到文件。"""
        if self.storage_path is None:
            return

        self.storage_path.mkdir(parents=True, exist_ok=True)
        trace_file = self.storage_path / f"{trace.trace_id}.json"

        with open(trace_file, "w") as f:
            json.dump(asdict(trace), f, ensure_ascii=False, indent=2, default=str)


class NoOpTracer:
    """空实现追踪器（禁用追踪时使用）。"""

    def start_trace(self, goal: str) -> str:
        return ""

    def end_trace(self, status: str = "completed") -> None:
        pass

    def start_span(self, name: str, kind: str = "step", **attributes: Any) -> None:
        pass

    def end_span(self, error: str | None = None, **attributes: Any) -> None:
        pass

    def record_llm_call(self, tokens: int, model: str = "") -> None:
        pass

    def record_tool_call(self, tool_name: str, success: bool = True) -> None:
        pass

    def get_trace(self, trace_id: str) -> None:
        return None

    def get_all_traces(self) -> list[Trace]:
        return []

    def get_summary(self) -> dict[str, Any]:
        return {}
