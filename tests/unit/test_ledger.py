"""P1 跨进程互斥账本：状态机 + 租约 + CAS 认领（防跨 worker 重复解题）。"""

from __future__ import annotations

from secpt.core.orchestrator.ledger import ChallengeLedger


class TestClaimMutex:
    def test_claim_atomic_exclusive(self, tmp_path):
        """同一题两个 worker 竞争：只有第一个 CAS 认领成功。"""
        db = tmp_path / "ledger.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        w1.ensure("g-05")
        assert w1.claim("g-05") is True
        assert w2.claim("g-05") is False  # 已被 w1 持有 → 不再认领（防重复解题）

    def test_completed_challenge_not_reclaimed(self, tmp_path):
        """已 solved 的题：其他 worker 不再认领（断点不重解）。"""
        db = tmp_path / "ledger.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        w1.ensure("g-06")
        w1.claim("g-06")
        w1.mark_running("g-06")
        w1.mark_solved("g-06", flags_found=2)
        assert w1.is_completed("g-06")
        assert w2.claim("g-06") is False
        assert w2.is_completed("g-06")

    def test_release_allows_reclaim(self, tmp_path):
        """释放租约（不结算）→ 回 queued，其他 worker 可接棒。"""
        db = tmp_path / "ledger.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        w1.ensure("g-07")
        w1.claim("g-07")
        w1.release("g-07")
        assert w2.claim("g-07") is True

    def test_expired_lease_auto_reclaimed(self, tmp_path):
        """租约超时未续（进程崩溃）→ 自动回 queued，其他 worker 可接棒。"""
        db = tmp_path / "ledger.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        w1.ensure("g-08")
        # 用负 TTL 模拟租约已过期
        assert w1.claim("g-08", lease_ttl_s=-1) is True
        assert w2.claim("g-08") is True  # 租约过期 → w2 接棒

    def test_heartbeat_renews_only_own_lease(self, tmp_path):
        """心跳只续自己的租约：w2 抢占后 w1 的心跳不生效。"""
        db = tmp_path / "ledger.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        w1.ensure("g-09")
        w1.claim("g-09", lease_ttl_s=-1)  # 租约立即过期
        w2.claim("g-09")  # w2 接棒
        assert w1.heartbeat("g-09") is False  # w1 已不持有 → 续租失败
        assert w2.heartbeat("g-09") is True  # w2 持有 → 续租成功


class TestStatus:
    def test_mark_running_and_exhausted(self, tmp_path):
        """状态流转：queued → claimed → running → exhausted（可重试）。"""
        led = ChallengeLedger(tmp_path / "l.sqlite", worker_id="w1")
        led.ensure("g-10")
        assert led.status("g-10") == "queued"
        assert led.claim("g-10")
        led.mark_running("g-10")
        assert led.status("g-10") == "running"
        led.mark_failed("g-10", reason="步数耗尽")
        assert led.status("g-10") == "exhausted"
        assert not led.is_completed("g-10")
        assert led.claim("g-10") is True  # exhausted 可重试

    def test_summary_counts(self, tmp_path):
        """账本汇总：各状态计数（跑分断点可视化）。"""
        led = ChallengeLedger(tmp_path / "l2.sqlite", worker_id="w1")
        for uc in ("a", "b"):
            led.ensure(uc)
        led.claim("a")
        led.mark_running("a")
        led.mark_solved("a")
        led.claim("b")
        led.mark_running("b")
        led.mark_failed("b")
        s = led.summary()
        assert s.get("solved") == 1 and s.get("exhausted") == 1

    def test_dump_json(self, tmp_path):
        """账本快照落盘（跑分复盘断点数据源）。"""
        led = ChallengeLedger(tmp_path / "l3.sqlite")
        led.ensure("g-11", flags_total=3)
        dest = tmp_path / "ledger.json"
        led.dump_json(dest)
        import json

        data = json.loads(dest.read_text())
        assert data[0]["unique_code"] == "g-11" and data[0]["flags_total"] == 3


class TestStateSync:
    """①/③ 状态同步：占用快照（holders/owns）+ 跨进程完成广播（scope_done）。"""

    def test_holders_snapshot(self, tmp_path):
        db = tmp_path / "l.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        w1.ensure("g-20")
        w1.claim("g-20")
        w1.mark_running("g-20")
        w2.ensure("g-21")
        w2.claim("g-21")
        h1 = w1.holders()
        assert h1["g-20"]["owned"] is True and h1["g-20"]["worker"] == "w1"
        assert h1["g-21"]["owned"] is False and h1["g-21"]["worker"] == "w2"
        assert h1["g-20"]["lease_left_s"] > 0

    def test_owns_gate(self, tmp_path):
        """close 所有权判定：只有持有租约的 worker 认 own。"""
        db = tmp_path / "l.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        w1.ensure("g-22")
        w1.claim("g-22")
        assert w1.owns("g-22") is True
        assert w2.owns("g-22") is False  # 无权关闭他人持有的题
        assert w2.owns("g-999") is False  # 未知题

    def test_expired_lease_not_owned(self, tmp_path):
        db = tmp_path / "l.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w1.ensure("g-23")
        w1.claim("g-23", lease_ttl_s=-1)
        assert w1.owns("g-23") is False  # 租约过期 → 不再持有，不得 close

    def test_scope_done_cross_process(self, tmp_path):
        """跨进程完成广播：w1 置位 → w2 可见（替代进程内 SharedStopper）。"""
        db = tmp_path / "l.sqlite"
        w1 = ChallengeLedger(db, worker_id="w1")
        w2 = ChallengeLedger(db, worker_id="w2")
        assert w2.is_scope_done("asset:x") is False
        w1.mark_scope_done("asset:x")
        assert w2.is_scope_done("asset:x") is True
        assert w1.is_scope_done("asset:x") is True
        w1.mark_scope_done("asset:x")  # 幂等：重复置位不报错
        assert w2.is_scope_done("asset:x") is True
        assert w2.is_scope_done("") is False  # 空 key 不误判
