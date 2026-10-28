"""M3-1 Guard 分级闸（先测试）：分级加载/保底/拦截/白名单/审计/全链。

蓝本锚点：拦截点=工具调用前（registry 唯一咽点，§2.1 L102 + WF3）；
agent_run 恒 HIGH 不可声明降级（§3.6 规则 4"Guard 同权"）；
白名单制最小权限（§7 安全表）。
三项拍板（§67）：配置入 profile.toml [guard] 段；HIGH 在 HITL（M3-2）到位前
fail-closed 拦截；缺 [guard] 段 = no-op 行为与 M1/M2 逐字节一致。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from secpt.core.loop import PentestLoop
from secpt.errors import SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import AgentRunTool
from secpt.tools.guard import GuardConfig, load_guard_config
from secpt.tools.registry import ToolManifest, ToolOutput, ToolRegistry

# ---------- 测试用工具替身（记账型：执行器是否被触碰可证） ---------------------------


class _EchoTool:
    """记账适配器：被 run 一次计一次数，回执固定——Guard 拦下则 calls 恒 0。"""

    manifest = ToolManifest(name="danger", kind="mcp", description="测试工具", params={})

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        self.calls += 1
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary="执行过一次",
            raw_ref="",
        )


# ---------- 装配：[guard] 段加载 ----------------------------------------------------


def test_guard_section_loads(tmp_path: Path) -> None:
    """tiers/whitelist 如实解析；未声明工具保底 low（白名单已放行才能到这）。"""
    path = tmp_path / "profile.toml"
    path.write_text(
        '[guard]\nwhitelist = ["danger", "agent_run", "nmap"]\n\n[guard.tiers]\ndanger = "mid"\n',
        encoding="utf-8",
    )
    cfg = load_guard_config(str(path))
    assert cfg is not None
    assert cfg.whitelist == ("danger", "agent_run", "nmap")
    assert cfg.tier_of("danger") == "mid"
    assert cfg.tier_of("nmap") == "low"


def test_guard_missing_section_is_none(tmp_path: Path) -> None:
    """拍板③：无文件/无 [guard] 段 → None（registry 闸常开，行为同 M1/M2）。"""
    assert load_guard_config(str(tmp_path / "ghost.toml")) is None
    path = tmp_path / "profile.toml"
    path.write_text("[judge]\nenabled = true\n", encoding="utf-8")
    assert load_guard_config(str(path)) is None


def test_guard_bad_tier_raises(tmp_path: Path) -> None:
    """tier 值拼错 = 配置笔误，同 judge 坏 toml 口径 E_PROFILE 拒跑。"""
    path = tmp_path / "profile.toml"
    path.write_text('[guard.tiers]\ndanger = "top"\n', encoding="utf-8")
    with pytest.raises(SecptError) as excinfo:
        load_guard_config(str(path))
    assert excinfo.value.code == "E_PROFILE"


# ---------- 分级语义 --------------------------------------------------------------


def test_agent_run_floor_is_high_and_not_lowerable() -> None:
    """§3.6 规则 4：agent_run 保底 HIGH；声明成 mid 也被保底顶回。"""
    cfg = GuardConfig(tiers={"agent_run": "mid", "danger": "low"})
    assert cfg.tier_of("agent_run") == "high"
    assert cfg.tier_of("danger") == "low"
    # hitl_mode=off（M3-1 fail-closed 语义锁定）：HIGH 不放行，声明降不了级。
    off = GuardConfig(tiers={"agent_run": "mid"}, hitl_mode="off")
    ok, _ = off.check("agent_run")
    assert ok is False


def test_registry_without_guard_is_noop() -> None:
    """registry 无 guard：与 M1/M2 行为逐字节一致（默认参数回归，204 例不跳红）。"""
    tool = _EchoTool()
    receipt = ToolRegistry([tool]).dispatch("danger", {})
    assert receipt.summary == "执行过一次"
    assert tool.calls == 1
    assert receipt.denied is False


# ---------- 拦截（fail-closed / 白名单） -------------------------------------------


def test_high_denied_before_execution() -> None:
    """fail-closed：HIGH 工具零执行（calls=0 可证），回执照出但 denied 无 raw_ref。

    hitl_mode=off 是 M3-1 fail-closed 语义的锁定（缺省 auto 属 M3-2 拍板，
    HITL 测试见 test_hitl.py）。
    """
    tool = _EchoTool()
    registry = ToolRegistry([tool], guard=GuardConfig(tiers={"danger": "high"}, hitl_mode="off"))
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 0
    assert receipt.denied is True
    assert receipt.exit_code is None
    assert receipt.raw_ref == ""
    assert receipt.summary.startswith("E_GUARD_DENY")


def test_whitelist_blocks_unlisted() -> None:
    """白名单制：已登记但不在线 → deny（最小权限，非黑名单）。"""
    tool = _EchoTool()
    registry = ToolRegistry([tool], guard=GuardConfig(whitelist=("nmap",)))
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 0
    assert receipt.denied is True
    assert "白名单" in receipt.summary


def test_mid_low_pass_through_unchanged() -> None:
    """放行路径：mid/low 的回执与 guard=None 逐字段一致（obs 行为不偏移）。"""
    plain = ToolRegistry([_EchoTool()]).dispatch("danger", {})
    tool = _EchoTool()
    guarded = ToolRegistry([tool], guard=GuardConfig(tiers={"danger": "mid"}))
    b = guarded.dispatch("danger", {})
    assert plain.model_dump() == b.model_dump()
    assert tool.calls == 1


# ---------- 全链：主循环拦截 + audit 落账 -------------------------------------------


def test_e2e_deny_lands_in_ledger() -> None:
    """FakeLLM 全链：agent_run（保底 HIGH）被拦 → 拒绝回执照落小票 + audit actor=main。

    双证断言：audit_log 有 E_GUARD_DENY 行；tickets 里 agent_run 小票绝无占位
    回执文案（执行器确实没被触碰）。
    """
    fake = FakeLLM(
        [
            json.dumps(
                {
                    "target": "10.0.0.5",
                    "objective": "试派一单",
                    "success_criteria": "agent_run 被拦回执",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "thought": "派单测试",
                    "plan_updates": [],
                    "tool_calls": [
                        {
                            "name": "agent_run",
                            "args": {
                                "mission": "试一试",
                                "persona": "recon",
                                "step_budget": "3",
                            },
                        }
                    ],
                    "done": False,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {"thought": "收工", "plan_updates": [], "tool_calls": [], "done": True},
                ensure_ascii=False,
            ),
            json.dumps({"summary": "deny e2e 收束"}, ensure_ascii=False),
        ]
    )
    conn = migrate_store.connect(":memory:")
    registry = ToolRegistry([AgentRunTool()], guard=GuardConfig(hitl_mode="off"))
    loop = PentestLoop(conn, MainLlm(LlmClient(fake)), registry, exit_checker=ExitChecklist())
    result = loop.run("deny 演示 10.0.0.5")

    assert result.finished_naturally is True
    rows = conn.execute(
        "SELECT actor, what FROM audit_log WHERE what LIKE 'E_GUARD_DENY%'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "main"
    assert "agent_run" in rows[0][1]
    tickets = conn.execute(
        "SELECT exit_code, raw_ref, summary FROM tickets WHERE tool_name='agent_run'"
    ).fetchall()
    assert len(tickets) == 1
    assert tickets[0][0] is None
    assert tickets[0][1] == ""
    assert tickets[0][2].startswith("E_GUARD_DENY")
