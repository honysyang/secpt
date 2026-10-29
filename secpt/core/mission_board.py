"""领单任务板（M3-5 拍板①）：MissionBoard 线程池 + Mission/MissionResult 票面。

为什么是线程池而不是进程池：多 worker 领单的并发对象是"LLM 器官 + 黑板写口"，
账本（sqlite 文件库）天然跨线程共享；进程/文件系统隔离交给 Docker 执行器
（拍板②），本层只管"单原子认领 + 回执一对一返回"。

认领为什么不加锁：queue.Queue 的 get 天然原子——单据在队列里排好序，worker
谁伸手谁领走，机械锁是多余的仪式。shutdown 语义三步钉死：正在跑的单善终、
队列里的余单清完、迟到的单当面拒绝（RuntimeError），三条各有测试钉着。

per-thread builder 时机：线程启动时 builder(index) 一次构建（Worker 内含
FakeLLM/registry 等非线程安全状态，绝不跨线程共享）；线程内逐单复用该实例。

task_id 解析（领单锚点不假写）：mission 没带 task_id 时经 task_resolver 从
账本查最新 task——查不到/没配置 resolver 都是同步 ValueError，绝不造空锚
把 worker 的 findings 写飞（账本红线：findings 必挂真 task）。
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from typing import Protocol

from pydantic import BaseModel, Field

_sentinel = object()


class Mission(BaseModel):
    """工单：worker 领单的最小契约面（goal 必填，task_id 由板级解析或显式携带）。

    ``skills``（M5）：技能库 id 清单——工单把"这单按哪套打法做"显式带上，
    而不是让 worker 自己猜。空元组 = 不挂技能（M3-5 老语义逐字节守恒）。

    ``coach_after_failures``（M5-13，设计文档 L11"何时 LLMC 教练员接入"）：
    连续工具失败达到该步数时允许 LLMC 介入一次；0 = 不接入（老语义）。
    """

    persona: str = "worker"
    goal: str
    step_budget: int = 8
    skills: tuple[str, ...] = ()
    coach_after_failures: int = 0
    task_id: str | None = None
    mission_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


class MissionResult(BaseModel):
    """收拢小票：worker 一次领干的如实回执（含预算消耗/案目数，拒绝吹牛字段）。"""

    mission_id: str
    ok: bool
    finished: bool
    steps_taken: int
    findings_count: int
    summary: str


class WorkerLike(Protocol):
    """worker 最小契约：run(mission) → 收拢小票（协议只锁方法不锁实现）。"""

    def run(self, mission: Mission) -> MissionResult:
        """领单干活。实现层自管异常降级；板级只兜底承载未接住的异常。"""
        ...


class MissionBoard:
    """领单任务板：size 个 worker 线程 + 原子认领队列 + future 一对一回执。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        worker_builder: Callable[[int], WorkerLike],
        *,
        size: int = 3,
        task_resolver: Callable[[sqlite3.Connection], str] | None = None,
        shutdown_grace_s: float = 30.0,
    ) -> None:
        """conn 只供 task_resolver 解析锚点（board 自身不落账）；resolver 返回 str。

        ``shutdown_grace_s``（M5-34）：收板 join 的宽限上限——collect 超时后
        worker 可能还在跑残留工单，join 无上限会让进程永远退不出去；宽限过后
        放弃等待（daemon 线程随进程退出，sqlite 事务本身原子，不会写坏账）。
        """
        self._conn = conn
        self._builder = worker_builder
        self._size = size
        self._resolver = task_resolver
        self._shutdown_grace_s = shutdown_grace_s
        self._queue: queue.Queue[tuple[Mission, Future[object]] | object] = queue.Queue()
        self._shutdown = False
        self._shutdown_lock = threading.Lock()
        self._build_error: str | None = None
        self._threads: tuple[threading.Thread, ...] = tuple(
            threading.Thread(
                target=self._work_loop, args=(index,), name=f"secpt-worker-{index}", daemon=True
            )
            for index in range(size)
        )
        for thread in self._threads:
            thread.start()

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        """worker 线程组（shutdown 后验活的观察窗）。"""
        return self._threads

    def submit(self, mission: Mission) -> Future[object]:
        """报单：锚点齐备才入队；board 收板/线程无法上岗时当面拒，不假收。"""
        if self._shutdown:
            raise RuntimeError("MissionBoard 已 shutdown：拒收新单")
        if self._build_error is not None:
            # M5-34：builder 失败 = 全体线程无法上岗，挂在队列里的单永远没人领——
            # 当面拒绝（AgentRunTool 转 E_TOOL_FAIL 小票），绝不让人等一个
            # 永远不会 resolve 的 future（悬挂 = 主循环永久卡死）。
            raise RuntimeError(f"worker 线程无法上岗：{self._build_error}")
        stamped = mission
        if stamped.task_id is None:
            if self._resolver is None:
                raise ValueError("submit 需要 resolver 或显式 task_id（领单锚点不假写）")
            try:
                stamped = stamped.model_copy(update={"task_id": self._resolver(self._conn)})
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"task_id 解析失败：{exc}") from exc
        future: Future[object] = Future()
        self._queue.put((stamped, future))
        return future

    def shutdown(self) -> None:
        """三步收板：排空余单（put 正好 size 个哨兵）→ 拒新单 → 线程收点 join。"""
        with self._shutdown_lock:
            if self._shutdown:
                return
            self._shutdown = True
            for _ in range(self._size):
                self._queue.put(_sentinel)
        for thread in self._threads:
            thread.join(timeout=self._shutdown_grace_s)

    def _work_loop(self, index: int) -> None:
        """worker 线程主环：起工一次 build → 循环原子领单 → 回执上票。

        M5-34：builder 抛错不再让线程无声死亡——记下原因、当面拒后续新单，
        队列里已入队的单立即置异常。旧口径"Mission 永远在路上且 future 永远
        等不到"就是把主循环永久卡死。
        """
        try:
            worker = self._builder(index)
        except Exception as exc:  # noqa: BLE001  # 装配失败有痕、有拒、不悬挂
            self._build_error = f"worker-{index} 装配失败 {type(exc).__name__}: {exc}"
            self._fail_pending(self._build_error)
            return
        while True:
            item = self._queue.get()
            try:
                if item is _sentinel:
                    return
                mission, future = item
                try:
                    future.set_result(worker.run(mission))
                except Exception as exc:  # noqa: BLE001  # 异常原样承载，板不死账不吞
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    def _fail_pending(self, reason: str) -> None:
        """排空队列：在板的单全部当面置异常（future 等待方拿到错误而非悬挂）。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _sentinel:
                continue
            _, future = item
            if not future.done():
                future.set_exception(RuntimeError(reason))


__all__ = ["Mission", "MissionBoard", "MissionResult", "WorkerLike"]
