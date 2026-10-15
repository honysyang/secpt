"""Phase 4 治理测试：G1 Scope / G2 限速 / G5 审计持久化。

注：G3/G4（RiskManager 高危确认）用例已随 risk.py 删除——该模块为死代码，
能力与 bash 白名单 + intercept 规则引擎重复。
"""

from __future__ import annotations

import pytest

from secpt.governance import (
    AuditLogger,
    RateLimiter,
    ScopeError,
    ScopeValidator,
)

# ---- G1: Scope ----


def test_scope_ip_exact() -> None:
    sv = ScopeValidator(["192.168.1.1"])
    assert sv.is_allowed("192.168.1.1")
    assert not sv.is_allowed("192.168.1.2")


def test_scope_cidr() -> None:
    sv = ScopeValidator(["192.168.1.0/24"])
    assert sv.is_allowed("192.168.1.1")
    assert sv.is_allowed("192.168.1.255")
    assert not sv.is_allowed("192.168.2.1")


def test_scope_domain() -> None:
    sv = ScopeValidator(["example.com"])
    assert sv.is_allowed("example.com")
    assert sv.is_allowed("sub.example.com")
    assert not sv.is_allowed("other.com")


def test_scope_validate_raises() -> None:
    sv = ScopeValidator(["10.0.0.0/8"])
    with pytest.raises(ScopeError):
        sv.validate("8.8.8.8")
    sv.validate("10.1.2.3")  # 不应抛


# ---- G2: Rate Limiter ----


@pytest.mark.asyncio
async def test_rate_limiter_acquire() -> None:
    rl = RateLimiter(max_requests=3, per_seconds=1.0)
    # 前 3 次立即获得
    for _ in range(3):
        await rl.acquire()
    # 第 4 次应等待
    import time

    start = time.monotonic()
    await rl.acquire()
    elapsed = time.monotonic() - start
    assert elapsed > 0.1  # 有等待


# ---- G5: Audit ----


def test_audit_log_and_read(tmp_path) -> None:
    al = AuditLogger(str(tmp_path / "audit.jsonl"))
    al.log("test.event", key="value", num=42)
    al.log("test.event2", flag=True)
    records = al.read_all()
    assert len(records) == 2
    assert records[0]["event"] == "test.event"
    assert records[0]["key"] == "value"
    assert records[1]["flag"] is True
    assert "ts" in records[0]


def test_audit_persistence(tmp_path) -> None:
    path = str(tmp_path / "audit.jsonl")
    al1 = AuditLogger(path)
    al1.log("first", data=1)
    al2 = AuditLogger(path)
    al2.log("second", data=2)
    records = al2.read_all()
    assert len(records) == 2
    assert records[0]["event"] == "first"
    assert records[1]["event"] == "second"
