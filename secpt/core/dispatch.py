"""保守批口径派单（M5-12 语义的唯一实现点，M5-31 起主循环与 worker 共用）。

为什么抽成共享函数：worker 内部此前是串行 for 循环，与主循环的批派单规则
（全批 parallel_safe 且开关开启才并发、混批整批串行、回执按提交序回填）各写
一份必然漂移。抽到这里后，"能不能并发"只有一处判据，两个调用方各自决定
``parallel_allowed`` 开关（主循环看全局态，worker 看探针是否在场）。

回执一律带 ``duration_ms``（M5-29 计时上移到本层）：工具自己不必关心耗时，
CLI 的"这张票跑了多久"与多并发时"谁在拖"都从这里来。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from secpt.llms.client import ToolCall
from secpt.tools.registry import ToolOutput


class Dispatcher(Protocol):
    """派单最小契约：dispatch + （可选）parallel_safe 查询。"""

    def dispatch(self, name: str, args: dict[str, str]) -> ToolOutput: ...


def dispatch_batch(
    dispatcher: Dispatcher,
    calls: Sequence[ToolCall],
    *,
    parallel_allowed: bool = True,
    thread_prefix: str = "secpt-dispatch",
) -> list[ToolOutput]:
    """派单一批工具调用：全批 parallel_safe 且开关开启才真并发。

    保守口径（为什么不是"能并的就并"）：只要批里混入一个非 parallel_safe
    工具，就整批串行——不同工具的副作用顺序不做重排，账本语义与串行逐字节
    一致。回执顺序恒等于调用顺序（并发结果按提交序回填），失速计收数与小票
    落账因此保持确定性。
    """
    safe_of = getattr(dispatcher, "parallel_safe", None)
    concurrent = (
        parallel_allowed
        and len(calls) >= 2
        and safe_of is not None
        and all(safe_of(call.name) for call in calls)
    )
    if not concurrent:
        return [_timed(dispatcher, call) for call in calls]
    with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix=thread_prefix) as pool:
        futures = [pool.submit(_timed, dispatcher, call) for call in calls]
        return [future.result() for future in futures]


def _timed(dispatcher: Dispatcher, call: ToolCall) -> ToolOutput:
    """单次派单 + 计时（串行与并发共用同一条路径，避免两套语义漂移）。"""
    started = time.monotonic()
    receipt = dispatcher.dispatch(call.name, {k: str(v) for k, v in call.args.items()})
    return receipt.model_copy(update={"duration_ms": int((time.monotonic() - started) * 1000)})


__all__ = ["dispatch_batch"]
