"""审批代理（M4-4，§71 遗留②）：worker 线程的 HIGH 动作经主线程代问。

为什么需要代理：CliApprover 直读 stdin——worker 线程里既没有终端也没有
抢 stdin 的资格（多线程读 stdin 会串行撕票）。任务书口径：worker 的 HIGH
动作经 ApprovalBroker 转发到主线程裁决（CLI prompt 档由主循环代问），批/
拒两路落 audit（workerA 发起 + 代理裁决 actor 分层可辨）；未装代理 →
fail-closed 保持 M3-5 现语义。

有界性（超时=拒绝，不悬死）：decide 的等待有 timeout_s 硬上限——主线程
serve 不及时（人不在/等待窗没开）worker 拿到拒绝继续干活，绝不永久挂起；
主线程晚到的答复只影响裁决行落账，不影响已发出的回执（future 置过值就
不再改判——回执一旦发出永不反悔）。

分层红线：broker 是 Approver 的实现者（tools 层），本身不落账——"批/拒
两路落 audit"由两个既有消化口收口：发起侧（worker loop，actor=workerA）
与裁决侧（on_decision 回调，main.py 落 actor=main）。broker 保持 ledger-free。
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from concurrent.futures import Future
from typing import Protocol

import structlog

from secpt.tools.guard import Approver

_log = structlog.get_logger(__name__)

DecisionSink = Callable[[str, bool, str], None]


class _DelegateLike(Protocol):
    """真审批人的最小结构面（kind 属性 + decide 方法，与 Approver 对齐）。"""

    kind: str

    def decide(self, name: str) -> bool: ...


class ApprovalBroker:
    """审批代理：worker 侧 decide 入队有界等待，主线程侧 serve_once 代问。

    kind 透传 delegate——账面诚实：裁决人是谁（human/auto）由真审批人
    署名，代理本身不冒充人类也不冒充自动。
    """

    def __init__(
        self,
        delegate: Approver,
        *,
        timeout_s: float = 60.0,
        on_decision: DecisionSink | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 必须为正数（有界性是构造期契约），got {timeout_s}")
        self._delegate: _DelegateLike = delegate
        self._timeout_s = timeout_s
        self._on_decision = on_decision
        self._queue: queue.Queue[tuple[str, Future[bool]]] = queue.Queue()

    @property
    def kind(self) -> str:
        """Approver 契约字段：透传真审批人的署名（registry hitl_note 用）。"""
        return self._delegate.kind

    def decide(self, name: str) -> bool:
        """worker 线程调用：入队 → 有界等待裁决；超时/链路异常一律拒绝。"""
        future: Future[bool] = Future()
        self._queue.put((name, future))
        try:
            return future.result(timeout=self._timeout_s)
        except TimeoutError:
            # concurrent.futures.TimeoutError 在 3.11+ 即内建 TimeoutError 本尊。
            _log.warning("approval_timeout_denied", tool=name, timeout_s=self._timeout_s)
            return False
        except Exception as exc:  # noqa: BLE001  转发链异常=拒绝（fail-closed，不悬死）
            _log.warning("approval_broker_failed_denied", tool=name, error=str(exc))
            return False

    def serve_once(self, *, timeout_s: float = 0.0) -> bool | None:
        """主线程调用：取一个待批请求交真审批人裁决；无请求返回 None。

        晚到答复（worker 已超时拿到拒绝）照常走 delegate 裁决并落账
        （on_decision），但不再改判 future——回执已发出永不反悔。
        """
        try:
            name, future = self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None
        try:
            decision = bool(self._delegate.decide(name))
        except Exception as exc:  # noqa: BLE001  真审批人炸了=拒绝（fail-closed）
            _log.warning("approval_delegate_failed_denied", tool=name, error=str(exc))
            decision = False
        if not future.done():
            future.set_result(decision)
        if self._on_decision is not None:
            try:
                self._on_decision(name, decision, self._delegate.kind)
            except Exception as exc:  # noqa: BLE001  落账失败不挡裁决链（broker 链路无账本依赖）
                _log.warning("approval_decision_sink_failed", tool=name, error=str(exc))
        return decision

    def pending(self) -> int:
        """待批请求数（观察窗：测试/CLI 展示用）。"""
        return self._queue.qsize()

    def deny_pending(self) -> int:
        """清场：未裁决请求全部当面拒绝（收板/关停时 worker 不悬死），返回笔数。"""
        denied = 0
        while True:
            try:
                _, future = self._queue.get_nowait()
            except queue.Empty:
                return denied
            if not future.done():
                future.set_result(False)
            denied += 1


__all__ = ["ApprovalBroker", "DecisionSink"]
