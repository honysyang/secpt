"""阶段三：per-asset 完成广播让出（SharedStopper + _YieldHook）的验收测试。

覆盖验收判据（计划文档阶段三 §1）：
- 同 asset_scope 已被其他意图 done → should_yield True（让出），自己结算过的不让出
- 不同资产互不干扰（per-asset 语义，非全局 stop）
- _YieldHook.on_step：inner 已有导引（watchdog nudge）时优先；stopper 命中返回
  协作广播且只注入一次（防刷屏）
"""

from __future__ import annotations

from secpt.core.orchestrator.frontier import SharedStopper, _YieldHook


class TestSharedStopper:
    def test_yield_same_asset_done_by_other(self):
        s = SharedStopper()
        s.mark_done("http://t:1", "int-aaa")
        assert s.should_yield("http://t:1", "int-bbb") is True  # 同资产被别的意图完成

    def test_self_done_not_yielded(self):
        s = SharedStopper()
        s.mark_done("http://t:1", "int-aaa")
        assert s.should_yield("http://t:1", "int-aaa") is False  # 自己不算让出对象

    def test_no_cross_asset_interference(self):
        s = SharedStopper()
        s.mark_done("http://t:1", "int-aaa")
        assert s.should_yield("http://t:2", "int-bbb") is False  # per-asset，非全局 stop
        assert s.should_yield("", "int-ccc") is False  # 无资产意图不参与

    def test_mark_done_without_scope_is_noop(self):
        s = SharedStopper()
        s.mark_done("", "int-aaa")
        assert s.should_yield("", "int-bbb") is False


class TestCrossProcessBroadcast:
    """③ 跨进程让出：完成广播落到账本 scope_done（其他 worker 进程同样可见）。"""

    def test_cross_process_yield(self, tmp_path):
        from secpt.core.orchestrator.ledger import ChallengeLedger

        db = tmp_path / "l.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        # w1 的 stopper 置位后，w2 的 stopper（换进程/换实例）经账本可见
        s2 = SharedStopper(cross=w2)
        assert s2.should_yield("asset:x", "int-me") is False
        w1.mark_scope_done("asset:x")
        assert s2.should_yield("asset:x", "int-me") is True

    def test_cross_query_failure_fail_open(self):
        """账本查询异常 → 不让出（fail-open），不阻断在途意图。"""

        class _Boom:
            def is_scope_done(self, key):
                raise RuntimeError("db gone")

        s = SharedStopper(cross=_Boom())
        assert s.should_yield("asset:x", "int-me") is False


class TestYieldHook:
    class _Inner:
        """最小 inner hooks 链（只实现 on_step + 一个自定义属性）。"""

        def __init__(self, steer=None):
            self._steer = steer
            self.extra = 42

        def on_step(self, steps, elapsed):
            return self._steer

    def test_inner_steer_priority(self):
        s = SharedStopper()
        s.mark_done("http://t:1", "int-done")
        h = _YieldHook(self._Inner(steer="watchdog nudge"), s, "http://t:1", "int-me")
        assert h.on_step(1, 0.1) == "watchdog nudge"  # 内部导引优先，不覆盖
        assert h._sent is False  # 未注入让出消息

    def test_yield_message_injected_once(self):
        s = SharedStopper()
        s.mark_done("http://t:1", "int-done")
        h = _YieldHook(self._Inner(), s, "http://t:1", "int-me")
        msg = h.on_step(1, 0.1)
        assert msg and "协作广播" in msg  # 命中 → 返回让出导引
        assert h.on_step(2, 0.2) is None  # 只注入一次，防刷屏
        assert h.on_step(3, 0.3) is None

    def test_no_yield_when_asset_not_done(self):
        h = _YieldHook(self._Inner(), SharedStopper(), "http://t:1", "int-me")
        assert h.on_step(1, 0.1) is None

    def test_delegates_other_attrs(self):
        inner = self._Inner()
        h = _YieldHook(inner, SharedStopper(), "a", "b")
        assert h.extra == 42  # __getattr__ 委托链不破（engine 按 hasattr 探测）
