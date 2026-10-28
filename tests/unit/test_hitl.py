"""M3-2 HITL 人工弹窗（先测试）：三档 hitl_mode + Approver 注入 + 批/拒两路落账。

蓝本锚点：§7 安全表"M3 Guard 分级(HIGH/MID/LOW)+人工弹窗"；施工结论史
L750"HITL v0.1 先落后端语义（CLI 提问等待），UI 后补"；§67 遗留①：
HIGH deny 分支改"提请人工"通道。
两拍板（§68）：hitl_mode 缺省 auto（非交互自动通过，mock 演示离线可复跑）；
approve 落账 audit actor=main（同拒绝路径，不新增 actor 破 M2 契约）。
设计边界：Guard 保持纯函数不碰 I/O——"人"以 Approver 注入 registry，
HIGH 级在执行器调用前过审批；批→执行且回执 hitl_approved=True，
拒→走 M3-1 denied 通道（拒绝语义零漂移）。
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
from secpt.tools.guard import AutoApprover, CliApprover, GuardConfig, load_guard_config
from secpt.tools.registry import ToolManifest, ToolOutput, ToolRegistry

# ---------- 测试替身 ----------------------------------------------------------------


class _EchoTool:
    """记账适配器：被 run 一次计一次数——HITL 拒下则 calls 恒 0（同 test_guard）。"""

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


class _FakeApprover:
    """脚本化审批替身：问过谁、批不批，全部可断言（prompt 档的假人类）。"""

    kind = "human"

    def __init__(self, verdict: bool) -> None:
        self.verdict = verdict
        self.asked: list[str] = []

    def decide(self, name: str) -> bool:
        self.asked.append(name)
        return self.verdict


# ---------- 配置：hitl_mode 三档 ----------------------------------------------------


def test_hitl_mode_default_is_auto() -> None:
    """拍板①：不写 hitl_mode = auto（非交互自动通过；mock 演示档即缺省）。"""
    assert GuardConfig().hitl_mode == "auto"


def test_hitl_mode_roundtrip(tmp_path: Path) -> None:
    """profile.toml [guard].hitl_mode 三档如实解析。"""
    for mode in ("prompt", "auto", "off"):
        path = tmp_path / f"{mode}.toml"
        path.write_text(f'[guard]\nhitl_mode = "{mode}"\n', encoding="utf-8")
        cfg = load_guard_config(str(path))
        assert cfg is not None
        assert cfg.hitl_mode == mode


def test_hitl_mode_invalid_raises(tmp_path: Path) -> None:
    """hitl_mode 拼错 = 配置笔误，同 tiers 坏值口径 E_PROFILE 拒跑。"""
    path = tmp_path / "profile.toml"
    path.write_text('[guard]\nhitl_mode = "yolo"\n', encoding="utf-8")
    with pytest.raises(SecptError) as excinfo:
        load_guard_config(str(path))
    assert excinfo.value.code == "E_PROFILE"
    assert "hitl" in str(excinfo.value)


# ---------- registry 弹窗闸：auto / prompt 批拒 / off / 无审批通道 --------------------


def test_auto_mode_runs_high_without_human() -> None:
    """auto 档：HIGH 直接过（缺省演示口径），回执带准旗 hitl_note=auto。"""
    tool = _EchoTool()
    registry = ToolRegistry(
        [tool], guard=GuardConfig(tiers={"danger": "high"}), approver=AutoApprover()
    )
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 1
    assert receipt.denied is False
    assert receipt.hitl_approved is True
    assert receipt.hitl_note == "auto"


def test_prompt_reject_denies_without_execution() -> None:
    """prompt 拒路：执行器零触碰，走 M3-1 denied 通道，拒绝文案可证。"""
    tool = _EchoTool()
    approver = _FakeApprover(False)
    registry = ToolRegistry([tool], guard=GuardConfig(tiers={"danger": "high"}), approver=approver)
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 0
    assert approver.asked == ["danger"]
    assert receipt.denied is True
    assert receipt.hitl_approved is False
    assert "拒绝" in receipt.summary
    assert receipt.summary.startswith("E_GUARD_DENY")


def test_prompt_approve_executes() -> None:
    """prompt 批路：批准即执行，回执带准旗 hitl_note=人工。"""
    tool = _EchoTool()
    approver = _FakeApprover(True)
    registry = ToolRegistry([tool], guard=GuardConfig(tiers={"danger": "high"}), approver=approver)
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 1
    assert approver.asked == ["danger"]
    assert receipt.denied is False
    assert receipt.hitl_approved is True
    assert receipt.hitl_note == "人工"


def test_off_never_consults_approver() -> None:
    """off 档：fail-closed 语义锁定——审批人装配了也不问，杜绝'装了就放行'。"""
    tool = _EchoTool()
    approver = _FakeApprover(True)
    registry = ToolRegistry(
        [tool],
        guard=GuardConfig(tiers={"danger": "high"}, hitl_mode="off"),
        approver=approver,
    )
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 0
    assert approver.asked == []
    assert receipt.denied is True
    assert receipt.summary.startswith("E_GUARD_DENY")


def test_missing_approver_fail_closed() -> None:
    """装配缺审批通道（guard 上班但 approver 没给）：保守拦下——不可静默放行。"""
    tool = _EchoTool()
    registry = ToolRegistry([tool], guard=GuardConfig(tiers={"danger": "high"}))
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 0
    assert receipt.denied is True
    assert "审批" in receipt.summary


def test_mid_never_consults_approver() -> None:
    """只有 HIGH 劳驾人：mid/low 直行，审批人未被问到且回执不带准旗。"""
    tool = _EchoTool()
    approver = _FakeApprover(True)
    registry = ToolRegistry([tool], guard=GuardConfig(tiers={"danger": "mid"}), approver=approver)
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 1
    assert approver.asked == []
    assert receipt.hitl_approved is False
    assert receipt.summary == "执行过一次"


# ---------- CliApprover：stdin 提问（蓝本 v0.1 形态） --------------------------------


def test_cli_approver_y_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """prompt 档真实现：stdin y/N——y 批准、其余一律视为拒绝（保守）。"""
    approver = CliApprover()
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert approver.decide("danger") is True
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert approver.decide("danger") is False
    monkeypatch.setattr("builtins.input", lambda _prompt: "Enter")
    assert approver.decide("danger") is False


# ---------- 全链：mock 批/拒两路各有账 ----------------------------------------------


def _fake_llm_scripts() -> FakeLLM:
    """M1 全链四条脚本：进场三问 / 派 agent_run / 收工 / 收束总结。"""
    return FakeLLM(
        [
            json.dumps(
                {
                    "target": "10.0.0.5",
                    "objective": "HITL 弹窗演示",
                    "success_criteria": "agent_run 过闸",
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
            json.dumps({"summary": "HITL e2e 收束"}, ensure_ascii=False),
        ]
    )


def test_e2e_auto_approve_lands_audit() -> None:
    """auto 档全链：HIGH agent_run 自动过闸执行 + audit 'HITL 批准' actor=main。"""
    conn = migrate_store.connect(":memory:")
    registry = ToolRegistry([AgentRunTool()], guard=GuardConfig(), approver=AutoApprover())
    loop = PentestLoop(
        conn, MainLlm(LlmClient(_fake_llm_scripts())), registry, exit_checker=ExitChecklist()
    )
    result = loop.run("HITL auto 演示 10.0.0.5")
    assert result.finished_naturally is True
    rows = conn.execute("SELECT actor, what FROM audit_log WHERE what LIKE 'HITL 批准%'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "main"
    assert "auto" in rows[0][1]
    assert "agent_run" in rows[0][1]


def test_e2e_prompt_reject_lands_audit() -> None:
    """prompt 拒路全链：人工拒绝即零执行 + audit 拒绝行与 executor 占位绝无交集。"""
    conn = migrate_store.connect(":memory:")
    registry = ToolRegistry([AgentRunTool()], guard=GuardConfig(), approver=_FakeApprover(False))
    loop = PentestLoop(
        conn, MainLlm(LlmClient(_fake_llm_scripts())), registry, exit_checker=ExitChecklist()
    )
    result = loop.run("HITL 拒绝演示 10.0.0.5")
    assert result.finished_naturally is True
    rows = conn.execute("SELECT actor, what FROM audit_log WHERE what LIKE '%人工拒绝%'").fetchall()
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
    assert "占位" not in tickets[0][2]
