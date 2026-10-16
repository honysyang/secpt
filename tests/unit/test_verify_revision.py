"""P4 验收（设计 docs/设计-工作循环形态.md §4.3/4.4）：验证异构 + 演化提案通道。

验收点：
- SECPT_VERIFY_MODEL 只作用于 verify 角色意图，其余角色恒用主模型；
- verify 的 role_stage 含"禁 read 旧 transcript"硬约束；
- _detect_goal_drift：题面外资产集中（>50% confirmed 无关 且 ≥2 条）才报；
- _queue_goal_revision：ask 模式登记 intercept pending 等人工审批 + 意图幂等；
- revision 意图确定性处置（blocked 等审批，不烧 LLM 步数、不起引擎子 job）。
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from secpt.core.job.fakellm import _resp
from secpt.core.orchestrator.frontier import (
    Frontier,
    _detect_goal_drift,
    _pick_intent_model,
    _queue_goal_revision,
    run_frontier,
)
from secpt.core.orchestrator.roles import role_stage


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_confirmed(var: str, base: str, target: str) -> None:
    from pathlib import Path

    from secpt.findings.models import Finding
    from secpt.findings.store import FindingStore

    f = Finding(
        title=f"{target} 存在漏洞", target=target, status="confirmed", job_id=f"{base}-i0000"
    )
    FindingStore(Path(var) / "findings.sqlite").save(f)


class TestVerifyModel:
    def test_verify_role_uses_env_model(self, monkeypatch):
        monkeypatch.setenv("SECPT_VERIFY_MODEL", "qwen3-coder")
        assert _pick_intent_model("verify", "deepseek-chat") == "qwen3-coder"

    def test_other_roles_ignore_env_model(self, monkeypatch):
        monkeypatch.setenv("SECPT_VERIFY_MODEL", "qwen3-coder")
        assert _pick_intent_model("exploit", "deepseek-chat") == "deepseek-chat"
        assert _pick_intent_model("", "deepseek-chat") == "deepseek-chat"

    def test_default_falls_back_to_primary(self, monkeypatch):
        monkeypatch.delenv("SECPT_VERIFY_MODEL", raising=False)
        assert _pick_intent_model("verify", "deepseek-chat") == "deepseek-chat"

    def test_verify_stage_bans_old_transcript(self):
        text = role_stage("verify")
        assert "禁止 read_file" in text and "旧证据只作线索不作依据" in text


class TestGoalDrift:
    def test_drift_when_unrelated_assets_dominate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        _seed_confirmed(str(tmp_path), "gd1", "http://sideb.example/login 存在注入")
        _seed_confirmed(str(tmp_path), "gd1", "http://sidec.example/api 未授权")
        _seed_confirmed(str(tmp_path), "gd1", "http://entry.example/ 正常")
        out = _detect_goal_drift("gd1", "目标：拿下 http://entry.example")
        assert out and "sideb" in out

    def test_no_drift_when_findings_related(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        _seed_confirmed(str(tmp_path), "gd2", "http://entry.example/login 存在注入")
        _seed_confirmed(str(tmp_path), "gd2", "http://entry.example/api 未授权")
        assert _detect_goal_drift("gd2", "目标：拿下 http://entry.example") is None

    def test_no_drift_with_single_unrelated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        _seed_confirmed(str(tmp_path), "gd3", "http://sideb.example/x 注入")
        _seed_confirmed(str(tmp_path), "gd3", "http://entry.example/y 正常")
        # 无关只有 1 条（<2）且未过半 → 不提案
        assert _detect_goal_drift("gd3", "目标：拿下 http://entry.example") is None


class TestRevisionQueue:
    def test_ask_mode_registers_pending_and_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        fr = Frontier(tmp_path / "f.db")
        ok1 = _queue_goal_revision(
            fr, base="rv1", objective="目标A", proposal="漂移证据……", mode="ask"
        )
        assert ok1
        ok2 = _queue_goal_revision(
            fr, base="rv1", objective="目标A", proposal="漂移证据……", mode="ask"
        )
        assert not ok2  # 幂等：已有未结算 revision 意图
        from secpt.governance.intercept import Interceptor

        pend = [p for p in Interceptor().pending_list() if p["target"] == "goal_revision"]
        assert len(pend) == 1 and "rv1" in pend[0]["job_id"]


class _EmptyPlanner:
    async def call(self, messages, tools, model):
        class _Msg:
            content = "[]"

        class _Resp:
            choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

        return _Resp()


class _IdleLLM:
    async def call(self, messages, tools, model):
        return _resp(
            {
                "role": "assistant",
                "content": "static",
                "tool_calls": [
                    {
                        "id": "x",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})},
                    }
                ],
            }
        )


class TestRevisionIntegration:
    def test_revision_intent_settles_blocked_without_engine(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_GOAL_REVISION", "off")  # 关闭自动探测，手动预置提案
        fr = Frontier(tmp_path / "fr.db")
        _queue_goal_revision(fr, base="rv2", objective="目标", proposal="证据", mode="ask")
        summary = _run(
            run_frontier(
                "目标：测试提案处置",
                llm=_IdleLLM(),
                planner_llm=_EmptyPlanner(),
                base_job_id="rv2",
                max_rounds=6,
                frontier=fr,
                max_intents=5,
            )
        )
        rev = [c for c in summary["chains"] if c.get("source") == "revision"]
        assert len(rev) == 1 and rev[0]["status"] == "blocked"
        # 确定性处置：revision 意图不起引擎子 job（不烧 LLM 步数）
        assert not any("revision" in str(j) for j in summary.get("sub_jobs", []))
