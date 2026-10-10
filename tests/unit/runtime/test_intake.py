"""intake 阶段测试。

覆盖：
- 假题面含内网 192.168.1.68 + 外网 http://10.30.15.1:54865 → targets 只有外网，内网只出现在 notes
- preflight 全失败 → 不产生 targets（不 spawn_root）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from secpt.core.llm.base import LLMMessage
from secpt.core.llm.fake import FakeLLM
from secpt.core.tools.registry import build_registry
from secpt.runtime.intake import intake


def _make_fake_llm(extract_json: str) -> FakeLLM:
    """构造返回固定 JSON 的 FakeLLM。"""
    return FakeLLM(
        script=[
            LLMMessage(role="assistant", content=extract_json),
        ]
    )


# ---------- 测试 1：假题面 → targets 只有外网，内网只在 notes ----------


@pytest.mark.asyncio
async def test_intake_filters_internal_addr(tmp_path: Path) -> None:
    """假题面含「内网 192.168.1.68 / 访问 http://10.30.15.1:54865」→
    targets == ["http://10.30.15.1:54865"]，192.168.1.68 只在 notes。
    """
    # 造假题目目录
    challenge_dir = tmp_path / "challenges" / "fake-challenge"
    challenge_dir.mkdir(parents=True)
    test_md = challenge_dir / "test.md"
    test_md.write_text(
        "SAML SSO 测试\n访问入口: http://10.30.15.1:54865\n内部服务: 192.168.1.68:8080\n",
        encoding="utf-8",
    )

    tools = build_registry()

    # FakeLLM 返回抽取 JSON
    llm = _make_fake_llm(
        '{"access": ["http://10.30.15.1:54865"], "internal_addr": ["192.168.1.68"], "attach": []}'
    )

    objective = f"解出 {challenge_dir}，找到 flag"

    # mock preflight：只让 http://10.30.15.1:54865 可达

    async def mock_preflight(target: str) -> bool:
        return "10.30.15.1" in target

    with patch("secpt.runtime.intake._preflight", side_effect=mock_preflight):
        result = await intake(objective, llm, tools)

    # 断言 targets
    assert result.targets == ["http://10.30.15.1:54865"], (
        f"targets 应只有 http://10.30.15.1:54865，实际: {result.targets}"
    )

    # 断言 192.168.1.68 只出现在 notes
    notes_str = "\n".join(result.notes)
    assert "192.168.1.68" in notes_str, f"192.168.1.68 应出现在 notes 中，notes: {result.notes}"
    assert "192.168.1.68" not in str(result.targets), "192.168.1.68 不应出现在 targets 中"

    # 断言 objective 包含题面全文
    assert "SAML SSO" in result.objective or "test.md" in result.objective, (
        "objective 应包含题面内容"
    )


# ---------- 测试 2：preflight 全失败 → targets 为空 ----------


@pytest.mark.asyncio
async def test_intake_all_unreachable_returns_empty_targets(tmp_path: Path) -> None:
    """preflight 全失败时 targets 应为空（不 spawn_root）。"""
    challenge_dir = tmp_path / "challenges" / "unreachable-challenge"
    challenge_dir.mkdir(parents=True)
    test_md = challenge_dir / "test.md"
    test_md.write_text(
        "访问入口: http://10.99.99.99:9999\n",
        encoding="utf-8",
    )

    tools = build_registry()

    llm = _make_fake_llm(
        '{"access": ["http://10.99.99.99:9999"], "internal_addr": [], "attach": []}'
    )

    objective = f"解出 {challenge_dir}，找到 flag"

    # mock preflight：全部不可达
    async def mock_preflight_always_fail(target: str) -> bool:
        return False

    with patch("secpt.runtime.intake._preflight", side_effect=mock_preflight_always_fail):
        result = await intake(objective, llm, tools)

    assert result.targets == [], f"preflight 全失败时 targets 应为空，实际: {result.targets}"

    # notes 应包含 unreachable 记录
    notes_str = "\n".join(result.notes)
    assert "unreachable" in notes_str, f"notes 应包含 unreachable 记录，notes: {result.notes}"


# ---------- 测试 2b：无 LLM 时正则也能抽出 URL ----------


@pytest.mark.asyncio
async def test_intake_regex_fallback_no_llm_extract() -> None:
    """LLM 抽取失败时，正则应能独立抽出 URL 作为 target。"""
    tools = build_registry()

    # FakeLLM 返回无效 JSON（触发解析失败 → 正则兜底）
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="not valid json"),
        ]
    )

    objective = "请扫描 http://10.30.15.1:54865 找到 flag"

    async def mock_preflight(target: str) -> bool:
        return True

    with patch("secpt.runtime.intake._preflight", side_effect=mock_preflight):
        result = await intake(objective, llm, tools)

    # 正则抽出的 URL 应作为 target
    assert "http://10.30.15.1:54865" in result.targets, (
        f"正则应抽出 URL 作为 target，targets: {result.targets}"
    )
