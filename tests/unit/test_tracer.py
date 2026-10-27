"""可观测性单元测试。"""

import pytest

from harness.observability.tracer import NoOpTracer, Span, Trace, Tracer


class TestSpan:
    def test_duration_ms(self):
        span = Span(name="test", kind="step", start_time=0)
        span.end_time = 0.5
        assert span.duration_ms == 500

    def test_duration_not_ended(self):
        span = Span(name="test", kind="step", start_time=0)
        assert span.duration_ms == 0


class TestTrace:
    def test_duration_ms(self):
        trace = Trace(trace_id="t1", goal="test", start_time=0)
        trace.end_time = 1.0
        assert trace.duration_ms == 1000


class TestTracer:
    def test_start_and_end_trace(self):
        tracer = Tracer()
        trace_id = tracer.start_trace("测试任务")
        tracer.end_trace()

        assert tracer.get_trace(trace_id) is not None
        assert tracer.get_trace(trace_id).status == "completed"

    def test_multiple_traces(self):
        tracer = Tracer()
        t1 = tracer.start_trace("任务1")
        tracer.end_trace()

        t2 = tracer.start_trace("任务2")
        tracer.end_trace()

        assert len(tracer.get_all_traces()) == 2

    def test_span_tracking(self):
        tracer = Tracer()
        tracer.start_trace("任务")
        tracer.start_span("llm_call", kind="llm_call", tokens=100)
        tracer.end_span()
        tracer.end_trace()

        trace = tracer.get_all_traces()[0]
        assert len(trace.spans) == 1
        assert trace.spans[0].kind == "llm_call"

    def test_summary(self):
        tracer = Tracer()
        tracer.start_trace("任务1")
        tracer.record_llm_call(tokens=100)
        tracer.record_tool_call("tool1")
        tracer.end_trace()

        tracer.start_trace("任务2")
        tracer.record_llm_call(tokens=50)
        tracer.end_trace(status="failed")

        summary = tracer.get_summary()
        assert summary["total_traces"] == 2
        assert summary["total_tokens"] == 150
        assert summary["completed"] == 1
        assert summary["failed"] == 1


class TestNoOpTracer:
    def test_noop(self):
        tracer = NoOpTracer()
        trace_id = tracer.start_trace("test")
        tracer.end_trace()
        tracer.start_span("test")
        tracer.end_span()
        tracer.record_llm_call(100)
        tracer.record_tool_call("tool")

        assert trace_id == ""
        assert tracer.get_all_traces() == []
        assert tracer.get_trace("any") is None
