"""统一理解智能体：多标签路由 + GoalSpec + 先验推荐（一次调用）+ 合并物化。"""

from __future__ import annotations

import asyncio
import json

from secpt.core.orchestrator.understanding import (
    MAX_PROFILES,
    understand_task,
)
from secpt.core.profile.loader import Profile, load_profile


def _resp(content: str):
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _R:
        def __init__(self, c):
            self.choices = [type("c", (), {"message": _Msg(c)})()]

    return _R(content)


class _JsonLLM:
    """按固定 JSON 回答一次；后续可复用于 solve 级联调。"""

    def __init__(self, payload: dict | str):
        self.payload = payload

    async def call(self, messages, tools, model):
        body = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False)
        )
        return _resp(f"```json\n{body}\n```")


class TestUnderstandTask:
    def test_unified_single_call(self):
        """一次调用同时产出分类与结构化目标（不再是两次 LLM）。"""
        llm = _JsonLLM(
            {
                "profiles": ["pentest"],
                "objective": "对 https://shop.example.com 授权测试，重点 IDOR 越权",
                "constraints": ["仅测该站", "当天出结论"],
                "asset_scope": ["https://shop.example.com"],
                "attack_steps": ["资产侦察", "越权点验证"],
                "priors": ["pentest.md"],
                "notes": "电商站+越权关键词",
            }
        )
        u = asyncio.run(understand_task("帮我测一下这个电商站", llm=llm))
        assert u.profiles == ["pentest"]
        assert "IDOR" in u.goal.objective
        assert u.goal.constraints == ["仅测该站", "当天出结论"]
        assert u.goal.asset_scope == ["https://shop.example.com"]
        assert u.priors == ["pentest.md"]  # 存在性已校验

    def test_multi_label_ordered_capped(self):
        llm = _JsonLLM(
            {
                "profiles": ["codeaudit", "pentest", "crypto", "pwn"],
                "objective": "x",
            }
        )
        u = asyncio.run(understand_task("源码+web 混合", llm=llm))
        assert u.profiles == ["codeaudit", "pentest", "crypto"][:MAX_PROFILES]
        assert u.profiles[0] == "codeaudit"  # 主标签序保持

    def test_benchmark_exclusive(self):
        llm = _JsonLLM({"profiles": ["benchmark", "pentest"], "objective": "x"})
        u = asyncio.run(understand_task("跑分", llm=llm))
        assert u.profiles == ["benchmark"]  # 跑分是程序化协议，独占

    def test_unknown_profile_and_prior_filtered(self):
        llm = _JsonLLM(
            {
                "profiles": ["superhacker", "ctf"],
                "objective": "x",
                "priors": ["nonexistent.md", "ctf.md"],
            }
        )
        u = asyncio.run(understand_task("夺旗", llm=llm))
        assert u.profiles == ["ctf"]  # 幻觉打法被白名单过滤
        assert u.priors == ["ctf.md"]  # 幻觉先验被过滤

    def test_fallback_on_garbage(self):
        """非 JSON 输出（含 FakeLLM 场景）→ 规则多标签 + 原文 GoalSpec。"""
        llm = _JsonLLM("这不是 JSON")
        u = asyncio.run(understand_task("这是一道栈溢出 pwn 题", llm=llm))
        assert u.profiles and u.profiles[0] in ("pwn", "ctf")  # 规则命中
        assert u.goal.objective == "这是一道栈溢出 pwn 题"

    def test_empty_profiles_falls_back_to_default(self):
        llm = _JsonLLM({"profiles": [], "objective": "杂项任务"})
        u = asyncio.run(understand_task("杂项任务", llm=llm))
        assert u.profiles == ["pentest"]  # 默认兜底


class TestMergeAndMaterialize:
    def test_merge_union_keeps_primary_semantics(self, tmp_path, monkeypatch):
        from secpt.core.orchestrator.understanding import merge_profiles

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        primary = load_profile("pentest")  # 主：角色/终止/预算
        codeaudit = load_profile("codeaudit")
        merged = merge_profiles(primary, [codeaudit])
        assert merged.name == "pentest"  # 主标签身份
        assert merged.budget.steps == primary.budget.steps  # 预算不动
        assert codeaudit.tools  # 次标签工具并入
        for t in codeaudit.tools:
            assert t in merged.tools
        assert "codeaudit.md" in [merged.prior, *merged.prior_extra]  # 先验并入
        assert "codeaudit" in merged.note  # 审计留痕

    def test_materialize_roundtrip(self, tmp_path, monkeypatch):
        from secpt.core.orchestrator.understanding import (
            materialize_merged_profile,
            merge_profiles_from_names,
        )

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        merged = merge_profiles_from_names(["ctf", "codeaudit"])
        path = materialize_merged_profile(merged, "job1")
        assert path.endswith("profiles/merged-job1.yaml")
        back = load_profile(path)  # 路径加载（loader 扩展）
        assert back.name == merged.name == "ctf"
        assert back.prior_extra == merged.prior_extra
        assert set(merged.tools) <= set(back.tools)

    def test_prior_text_concatenates_extra(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        p = Profile(name="t", prior="ctf.md", prior_extra=["pentest.md"])
        text = p.prior_text()
        assert text  # 两段真实先验串联（仓库内置 priors 存在）
        assert len(text) > len((p.prior_path).read_text(encoding="utf-8"))


class TestSemanticPrior:
    def _real_prior(self, name: str) -> str:
        from secpt.cli.solve import _PRIOR_MAP

        return (_PRIOR_MAP.parent / "priors" / f"{name}.md").read_text(encoding="utf-8")

    def test_keyword_still_wins(self):
        from secpt.cli.solve import _auto_prior

        assert _auto_prior("目标使用 SAML 单点登录") == "saml"

    def test_semantic_fallback_with_fake_embedder(self, monkeypatch):
        """关键词全不命中时语义兜底：objective 与某先验关键词向量对齐 → 命中。"""
        from secpt.cli import solve as solve_mod
        from secpt.knowledge import embedding as emb

        emb.reset_for_tests()
        try:

            def fake(texts):
                out = []
                for t in texts:
                    # 含"逻辑缺陷"或 saml 关键词串 → 同向；其余反向（含先验名）
                    hot = ("逻辑缺陷" in t) or ("单点登录" in t and "saml " in t.lower())
                    out.append([1.0, 0.0] if hot else [0.0, 1.0])
                return out

            emb.set_embedder(fake)
            hit = solve_mod._auto_prior("系统存在逻辑缺陷，越权访问他人订单")
            assert hit == "saml"  # 由 semantic 路径（score=1.0）命中
        finally:
            emb.reset_for_tests()

    def test_semantic_disabled_returns_none(self, monkeypatch):
        from secpt.cli import solve as solve_mod
        from secpt.knowledge import embedding as emb

        emb.reset_for_tests()
        monkeypatch.setenv("SECPT_EMBED", "0")
        try:
            assert solve_mod._auto_prior("完全无关的文本内容") is None
        finally:
            emb.reset_for_tests()


def test_run_engine_accepts_merged_profile_path(tmp_path, monkeypatch):
    """下游贯通：run_engine 按 profile 路径加载合并产物（字符串管道零改动）。"""
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    from secpt.cli.engine_runner import run_engine
    from secpt.core.orchestrator.understanding import (
        materialize_merged_profile,
        merge_profiles_from_names,
    )

    merged = merge_profiles_from_names(["ctf", "codeaudit"])
    path = materialize_merged_profile(merged, "mp1")
    result = run_engine(objective="测试目标", profile=path, job_id="mp1", max_steps=3)
    assert result.steps >= 1  # 引擎正常跑通（按路径加载 profile）
