"""漂移指标（S12 约束 E）：全部从 derive_stats 重算的 meta 取，纯函数。

- thrashing：max(task.attempt)——与阈值比由 coach 做，这里只给实际值；
- vs_rate：vs_events / 已返回 worker 数（事件口径，教练用）；
- authz_ratio：AuthZ 失败 / 总失败（failures_by_class 来自 derive_stats）。
"""

from __future__ import annotations

from pa.models import MissionState


def thrashing(state: MissionState) -> int:
    """本 mission 最大的任务尝试次数。"""
    return max((t.attempt for t in state.tasks.values()), default=0)


def vs_rate(meta: dict) -> float:
    """vs_events（derive_stats['virtual_success_count']）/ worker.returned 数。"""
    returns = int(meta.get("worker_returns", 0))
    if not returns:
        return 0.0
    return int(meta.get("virtual_success_count", 0)) / returns


def authz_ratio(meta: dict) -> float:
    fbc = meta.get("failures_by_class", {})
    total = sum(fbc.values())
    if not total:
        return 0.0
    return int(fbc.get("AuthZ", 0)) / total
