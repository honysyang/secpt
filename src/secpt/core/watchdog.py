"""S3 成果递减监控 watchdog。

每回合（session rollover 边界）统计新增 assets/credentials/findings 数量，
连续 3 个回合零新增 → status=paused + 告警，并返回"换层"提示，
由 Engine 注入下一回合的交接 user 消息（system_extra 语义）。

7×24 最危险的不是失败，是安静地空转一整天。

实现：包装 EventSink hooks（__getattr__ 透传），在 on_finding 计数去重，
在 on_rollover 评估回合边界。Engine 用 hooks.on_rollover(...) 的返回值
拿到换层提示。
"""

from __future__ import annotations

import contextlib

MAX_IDLE_ROUNDS = 3

REDIRECT_HINT = (
    "【换层提示】已连续 {n} 个回合没有新增资产/凭据/发现。当前思路可能已走到"
    "死胡同，请切换侦察层次（例如从端口扫描转向 Web 应用层、从暴力破解转向协议"
    "逻辑漏洞、从单一工具转向组合利用），并重新确认目标面。"
)


class Watchdog:
    def __init__(
        self, inner, *, store=None, job_id: str = "", max_idle_rounds: int = MAX_IDLE_ROUNDS
    ) -> None:
        self._inner = inner
        self._store = store
        self._job_id = job_id
        self._max_idle = max_idle_rounds
        self._seen: set[tuple[str, str]] = set()  # (kind, value) 去重
        self._new_this_round = 0
        self._idle_rounds = 0
        self._paused = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # ---------------- 观察点 ----------------

    def on_finding(self, kind: str, value: str, evidence: str = "") -> None:
        """Engine 报告新发现：去重计数 + 透传给 EventSink。"""
        key = (kind, str(value))
        if key not in self._seen:
            self._seen.add(key)
            self._new_this_round += 1
        # 一旦有新产出，paused → running
        if self._paused:
            self._unpause()
        inner = getattr(self._inner, "on_finding", None)
        if inner is not None:
            inner(kind, value, evidence)
        elif hasattr(self._inner, "finding"):
            self._inner.finding(kind, value, evidence)  # 兼容旧签名

    def on_rollover(self, round_: int, messages: list[dict], handoff: str = ""):
        """回合边界评估：返回"换层"提示（空串表示无需干预）。

        先透传内层（落盘 rollover 事件+round 快照），再统计本回合新增。
        """
        inner = getattr(self._inner, "on_rollover", None)
        if inner is not None:
            inner(round_, messages, handoff)
        elif hasattr(self._inner, "rollover"):
            self._inner.rollover(round_, messages, handoff)

        hint = ""
        if self._new_this_round == 0:
            self._idle_rounds += 1
        else:
            self._idle_rounds = 0
        self._new_this_round = 0
        if self._idle_rounds >= self._max_idle:
            hint = REDIRECT_HINT.format(n=self._idle_rounds)
            if not self._paused:
                self._pause()  # 只标记一次（软暂停状态已足够），避免每回合重复刷事件
        return hint

    # ---------------- 内部 ----------------

    def _pause(self) -> None:
        """置 paused + 告警（软暂停：Engine 继续跑，换层提示引导方向）。"""
        self._paused = True
        print(
            f"[watchdog] job={self._job_id} 连续 {self._idle_rounds} 回合零新增，"
            "标记 paused + 注入换层提示"
        )
        with contextlib.suppress(Exception):
            from secpt.runtime.notify import notify

            notify(
                "task_paused",
                f"secpt 任务 {self._job_id} 空转暂停",
                f"连续 {self._idle_rounds} 回合无新增产出（换层提示已注入）",
            )
        if hasattr(self._inner, "status_change"):
            self._inner.status_change("paused")
        if self._store is not None:
            try:
                rec = self._store.get_job(self._job_id)
                if rec is not None:
                    rec.set_status("paused")
                    self._store.upsert_job(rec)
            except Exception as e:
                print(f"[watchdog] pause 落库失败(忽略): {e}")

    def _unpause(self) -> None:
        self._paused = False
        if self._store is not None:
            try:
                rec = self._store.get_job(self._job_id)
                if rec is not None and rec.status == "paused":
                    rec.set_status("running")
                    self._store.upsert_job(rec)
            except Exception as e:
                print(f"[watchdog] unpause 落库失败(忽略): {e}")
