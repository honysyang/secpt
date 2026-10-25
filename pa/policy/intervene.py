"""enforce 动作（S12 约束 F）：只三种——supersede / 提前收口 / 降级 model。

只在 mode=="enforce" 时由 coach 调用，每个动作落事件：
- supersede_task：transition → task.state_changed（重放可还原）+
  mentor.intervened 记录动作本身；
- settle_plateau：mentor 写 mission.finished{reason: "plateau"}——与 loop 的
  收口共用幂等键 mf:{mission_id}，先到先得，不会重复收口；
- degrade_model：model.switched 事件（v0 无降级链执行机构，事件即动作，
  供 trace/stats 观测）。

不实现 I3 提醒、不实现 I5 升级（没有投递对象）。
"""
from __future__ import annotations

from pa.graph.state import transition
from pa.models import MissionState
from pa.store.event import apply_event
from pa.store.writer import StoreWriter


def supersede_task(state: MissionState, writer: StoreWriter, task,
                   issue: str, metric: dict) -> None:
    if task.state != "failed":            # 已 supersede/done 的不重复动作
        return
    transition(task, "superseded", writer)
    writer.append("mentor.intervened", "mentor",
                  {"level": "I2", "issue": issue,
                   "detail": {"action": "supersede", "task_id": task.id,
                              "metric": metric}},
                  f"int:supersede:{task.id}")


def settle_plateau(state: MissionState, writer: StoreWriter,
                   satisfied: list[str]) -> None:
    payload = {"summary": f"mentor 提前收口（authz_ratio 越线）：验收满足 "
                          f"{len(satisfied)}/{len(state.spec.acceptance)}",
               "reason": "plateau", "satisfied": satisfied}
    ev = writer.append("mission.finished", "mentor", payload,
                       f"mf:{state.mission_id}")
    apply_event(state, ev)


def degrade_model(state: MissionState, writer: StoreWriter,
                  to_model: str, metric: dict) -> None:
    writer.append("model.switched", "mentor",
                  {"to": to_model, "reason": "vs_rate", "metric": metric},
                  f"sw:vs_rate:{state.mission_id}")
