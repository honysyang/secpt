"""Phase 4 治理测试：G1 Scope / G2 限速 / G3 高危确认 / G4 分级 / G5 审计持久化。"""

from __future__ import annotations

import pytest

from secpt.governance import (
    AuditLogger,
    ConfirmationRequired,
    RateLimiter,
    RiskLevel,
    RiskManager,
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


# ---- G3 + G4: Risk ----


def test_risk_levels() -> None:
    rm = RiskManager()
    assert rm.get_risk("echo") == RiskLevel.READONLY
    assert rm.get_risk("nmap") == RiskLevel.MEDIUM
    assert rm.get_risk("nikto") == RiskLevel.HIGH


def test_needs_confirmation() -> None:
    rm = RiskManager()
    assert not rm.needs_confirmation("echo")
    assert rm.needs_confirmation("nikto")


def test_confirm_required_raises() -> None:
    rm = RiskManager()
    with pytest.raises(ConfirmationRequired):
        rm.confirm("nikto", "-h 127.0.0.1", approved=False)


def test_confirm_approved() -> None:
    rm = RiskManager()
    rm.confirm("nikto", "-h 127.0.0.1", approved=True)  # 不应抛


def test_low_risk_no_confirmation() -> None:
    rm = RiskManager()
    rm.confirm("echo", "hello", approved=False)  # 不应抛


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
