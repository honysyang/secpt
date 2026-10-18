"""意图授权白名单：objective 实际目标 ∪ host 形态 asset_scope（tsec1-1036 误拦修复）。

asset_scope 是题号（c-03）不是网络目标——直接当 scope 会把对自己容器的每次
请求都拦掉（1036：8 分钟 24 次 scope.denied），模型被逼写脚本绕过治理链。
"""

from __future__ import annotations

from types import SimpleNamespace

from secpt.core.orchestrator.frontier import _intent_scope_entries


def test_container_addr_extracted_code_excluded():
    intent = SimpleNamespace(
        objective="目标: http://10.0.176.40:80 (a-05 容器)。api.py start 已内置自动腾位",
        asset_scope="a-05",
    )
    entries = _intent_scope_entries(intent)
    assert entries and "10.0.176.40" in entries
    assert "a-05" not in entries  # 题号不是网络目标，不进白名单


def test_hostlike_asset_scope_merged_dedup():
    intent = SimpleNamespace(
        objective="对 target.example.com 做安全测试",
        asset_scope="target.example.com",
    )
    entries = _intent_scope_entries(intent)
    assert entries.count("target.example.com") == 1  # 合并且去重


def test_code_only_scope_returns_none():
    intent = SimpleNamespace(objective="", asset_scope="c-03")
    assert _intent_scope_entries(intent) is None


def test_no_targets_returns_none():
    intent = SimpleNamespace(objective="本地整理笔记", asset_scope="")
    assert _intent_scope_entries(intent) is None
