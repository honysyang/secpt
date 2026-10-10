"""S5 Warroom（L2 共享作战记忆）单测。

覆盖：四表增查 / attempts 去重（sha1 key）/ verified 标记。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secpt.core.memory.warroom import Warroom, attempt_key


@pytest.fixture
def warroom(tmp_path: Path) -> Warroom:
    return Warroom(tmp_path / "warroom.sqlite", job_id="test-job")


# ---------------- assets ----------------


def test_assets_roundtrip(warroom: Warroom) -> None:
    warroom.add_asset("127.0.0.1", 8001, "http", "nginx", source_step="recon")
    warroom.add_asset("127.0.0.1", 8002, "http", "", source_step="recon")

    rows = warroom.list_assets()
    assert len(rows) == 2
    assert rows[0]["host"] == "127.0.0.1"
    assert rows[0]["port"] == 8001
    assert rows[0]["service"] == "http"
    assert rows[0]["source_step"] == "recon"
    assert warroom.count_assets() == 2


# ---------------- credentials ----------------


def test_credentials_roundtrip(warroom: Warroom) -> None:
    warroom.add_credential("regex", "password=admin123", source_step="step 3")
    warroom.add_credential("manual", "token=abc", valid=True)

    rows = warroom.list_credentials()
    assert len(rows) == 2
    assert rows[1]["kind"] == "manual"
    assert rows[1]["valid"] == 1


# ---------------- findings + verified ----------------


def test_findings_and_verify(warroom: Warroom) -> None:
    warroom.add_finding("f1", "flag", "FLAG{abc}", evidence="exploit 自宣告", severity="critical")
    warroom.add_finding("f2", "credential", "password=x", evidence="", severity="low")

    assert warroom.count_verified() == 0
    # evidence 非空的乐观确认
    for f in warroom.list_findings():
        if f["evidence"].strip():
            warroom.mark_verified(f["id"], True)
    assert warroom.count_verified() == 1

    # 幂等更新
    warroom.add_finding("f1", "flag", "FLAG{abc2}", evidence="new", severity="critical")
    rows = warroom.list_findings()
    assert len(rows) == 2  # 不重复
    assert rows[0]["title"] == "FLAG{abc2}"
    assert rows[0]["verified"] == 1


# ---------------- attempts 去重 ----------------


def test_attempt_key_deterministic() -> None:
    assert attempt_key("bash(ls)", "http://127.0.0.1:8001") == attempt_key(
        "bash(ls)", "http://127.0.0.1:8001"
    )
    assert attempt_key("bash(ls)", "http://127.0.0.1:8001") != attempt_key(
        "bash(ls)", "http://127.0.0.1:8002"
    )


def test_attempts_dedupe(warroom: Warroom) -> None:
    action = 'bash({"command": "ls /tmp"})'
    target = "http://127.0.0.1:8001"

    first = warroom.add_attempt(action, target=target, result="ok")
    dup = warroom.add_attempt(action, target=target, result="ok")
    other = warroom.add_attempt(action, target="http://127.0.0.1:8002")

    assert first is True
    assert dup is False  # 同 action+target 重复 → 跳过
    assert other is True
    assert warroom.count_attempts() == 2
    assert warroom.has_attempt(action, target=target) is True
    assert warroom.has_attempt(action, target="http://127.0.0.1:9999") is False
