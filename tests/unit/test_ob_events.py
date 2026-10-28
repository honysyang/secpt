"""EventBus contract（观测层最小语义：转发 + 订阅者异常不冒泡）。"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import pytest

from secpt.ob.events import EventBus


def test_subscribe_then_emit_receives_name_and_payload() -> None:
    received: list[tuple[str, Mapping[str, object]]] = []
    bus = EventBus()
    bus.subscribe(lambda name, payload: received.append((name, payload)))
    bus.emit("run_step_started", task_id="t1", step_no=3)
    assert len(received) == 1
    name, payload = received[0]
    assert name == "run_step_started"
    assert payload["event"] == "run_step_started"
    assert payload["task_id"] == "t1"
    assert payload["step_no"] == 3


def test_emit_fans_out_to_all_subscribers_in_order() -> None:
    order: list[str] = []
    bus = EventBus()
    bus.subscribe(lambda _n, _p: order.append("first"))
    bus.subscribe(lambda _n, _p: order.append("second"))
    bus.emit("ticket_appended", ticket_id="x")
    assert order == ["first", "second"]


def test_subscriber_exception_is_swallowed_not_propagated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """观测炸了不得停主循环（§9：循环悬死是明令禁止状态）。"""

    def boom(_name: str, _payload: Mapping[str, object]) -> None:
        raise RuntimeError("订阅者内部崩溃")

    received: list[str] = []
    bus = EventBus()
    bus.subscribe(boom)
    bus.subscribe(lambda name, _p: received.append(name))
    with caplog.at_level(logging.ERROR, logger="secpt.ob.events"):
        bus.emit("exit_passed", task_id="t")
    assert received == ["exit_passed"]
    assert any("订阅者" in r.message or "subscriber" in r.message for r in caplog.records)


def test_emit_without_subscribers_is_noop() -> None:
    bus = EventBus()
    bus.emit("run_ended")  # 不应抛错
