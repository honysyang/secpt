"""全局提效约束：一轮并发多工具（engine_runner 装配 constraints 时统一追加）。"""

from __future__ import annotations

from secpt.cli.engine_runner import _with_batch_constraint
from secpt.core.profile.loader import PROFILE_BATCH_CONSTRAINT


def test_appended_when_missing():
    out = _with_batch_constraint(["不要扫描题面外的网段。"])
    assert out[0] == "不要扫描题面外的网段。"  # 原约束保留在前
    assert out[1] == PROFILE_BATCH_CONSTRAINT
    assert "多个 tool_calls" in PROFILE_BATCH_CONSTRAINT


def test_idempotent():
    once = _with_batch_constraint(["不要扫描题面外的网段。"])
    twice = _with_batch_constraint(once)
    assert twice == once  # 已含"一轮"字样不重复追加
    assert len(twice) == 2


def test_empty_list_gets_constraint_only():
    assert _with_batch_constraint([]) == [PROFILE_BATCH_CONSTRAINT]
