"""S4 两级路由单测：五类典型输入各 3 条，规则层命中率 >= 80%。

规则层（llm=None 时不触发 LLM 兜底）：
- ctf / redteam / codeaudit / assessment 关键词命中 → 对应 profile
- 未命中 → 默认 pentest
"""

from __future__ import annotations

import asyncio

import pytest

from secpt.core.profile.router import route_objective

# 五类 × 3 条：前三类靠规则关键词，pentest 靠"未命中→默认"
CASES: dict[str, list[str]] = {
    "ctf": [
        "这道 CTF 有个 flag，帮我拿下它",
        "逆向题目，pwn 一道，找出 flag{...}",
        "挑战：解题思路，目标拿到 RCTF{...}",
    ],
    "redteam": [
        "红队对抗演练，模拟 APT 攻击链",
        "做一次横向移动 + 权限维持的红队测试",
        "攻防演练：钓鱼 + 攻击链复盘",
    ],
    "codeaudit": [
        "代码审计 /tmp/audit-target，找 SQL 注入",
        "源码审计：按 sink 反查危险函数调用链",
        "审查代码，找出路径穿越漏洞并给 file:line",
    ],
    "assessment": [
        "做一次等保合规评估",
        "安全评估：按检查清单逐项验证",
        "基线加固评估，输出风险清单",
    ],
    "pentest": [
        "扫描这台服务器的开放端口",
        "对目标 web 应用做一次渗透测试",
        "枚举目标主机上的服务并尝试弱口令",
    ],
}


def _route(objective: str) -> str:
    return asyncio.run(route_objective(objective))


@pytest.mark.parametrize("expected,texts", [(k, v) for k, v in CASES.items()])
def test_rule_route(expected: str, texts: list[str]) -> None:
    for text in texts:
        assert _route(text) == expected, f"{text!r} 应路由到 {expected}"


def test_rule_hit_rate_at_least_80() -> None:
    """总命中率（正确分类 / 全部输入）>= 80%。"""
    hits = total = 0
    for expected, texts in CASES.items():
        for text in texts:
            total += 1
            if _route(text) == expected:
                hits += 1
    rate = hits / total
    assert rate >= 0.8, f"命中率 {rate:.0%} < 80%（{hits}/{total}）"


def test_routed_event_written(tmp_path) -> None:
    """路由结果必须落 events（type=routed）。"""
    from secpt.core.job.event import EventSink

    sink = EventSink("test-router-job", tmp_path)
    profile = asyncio.run(
        route_objective(
            "红队攻击链测试，目标内网横向移动",
            sink=sink,
            job_id="test-router-job",
            round_=1,
        )
    )
    assert profile == "redteam"
    ep = tmp_path / "test-router-job" / "events.jsonl"
    assert ep.is_file()
    lines = [ln for ln in ep.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any('"routed"' in ln and '"profile": "redteam"' in ln for ln in lines)
