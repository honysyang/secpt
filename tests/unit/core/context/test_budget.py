"""test_budget：预算 80%/100% 阈值；100% 优雅停（findings 落盘，status=budget_exceeded）。"""

from __future__ import annotations

import json

from secpt.core.context.budget import WARN_80_MSG, BudgetTracker, tokens_cost
from secpt.core.job.event import EventSink
from secpt.core.job.store import JobStore


def test_warn_then_exceed_without_exception() -> None:
    bt = BudgetTracker(cost_limit_cny=1.0, input_per_1k=0.002, output_per_1k=0.008)
    # 一次性灌到接近 100%：prompt=250k+125k 输出
    bt.add(250_000, 100_000)  # 250*0.002 + 100*0.008 = 0.5 + 0.8 = 1.3 ≥ 1.0
    assert bt.check() == "exceeded"
    assert bt.exceeded is True
    assert bt.cost_cny > 1.0
    # 全程没有抛异常（优雅停的前提）
    assert bt.percent() > 1.0


def test_warn_fires_once() -> None:
    bt = BudgetTracker(cost_limit_cny=1.0, input_per_1k=0.002, output_per_1k=0.008)
    bt.add(180_000, 60_000)  # 180*0.002 + 60*0.008 = 0.36+0.48 = 0.84 → 84% ≥ 80%
    assert bt.check() == "warn"
    assert bt.check() == ""  # 只 warn 一次
    bt.add(200_000, 0)  # 继续灌到超
    assert bt.check() == "exceeded"


def test_unlimited_budget_never_triggers() -> None:
    bt = BudgetTracker(cost_limit_cny=0.0)  # 0 = 不限
    bt.add(10_000_000, 10_000_000)
    assert bt.check() == ""
    assert bt.percent() == 0.0


def test_tokens_cost_matches_config() -> None:
    # 单价：input 0.002 / output 0.008（CNY per 1K）
    assert abs(tokens_cost(1000, 1000) - (0.002 + 0.008)) < 1e-9


def test_budget_exceeded_graceful_flush(tmp_path) -> None:
    """100% 时：findings 已落盘、report.md 生成、status=budget_exceeded。"""
    store = JobStore(tmp_path / "t.sqlite")
    sink = EventSink("j1", tmp_path / "jobs", store=store)
    sink.on_finding("credential", "admin:Passw0rd!", "step 5: curl")
    messages = [{"role": "system", "content": "s"}, {"role": "assistant", "content": "done"}]
    sink.on_budget_exceeded(messages)

    # report.md 已生成并含 finding
    report = tmp_path / "jobs" / "j1" / "report.md"
    assert report.is_file()
    content = report.read_text(encoding="utf-8")
    assert "admin:Passw0rd!" in content
    assert "budget_exceeded" in content

    # 事件流里有 status_change(budget_exceeded)
    events = [
        json.loads(ln)
        for ln in (tmp_path / "jobs" / "j1" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.strip()
    ]
    assert any(
        e["type"] == "status_change" and e["payload"]["status"] == "budget_exceeded" for e in events
    )
    # SQLite findings 已落盘
    assert len(store.list_findings("j1")) == 1


def test_warn_message_text() -> None:
    assert "80%" in WARN_80_MSG
    assert "结构化结论" in WARN_80_MSG


class TestPerProviderPricing:
    """D 修复：per-provider 计价（池化后预算百分比不再按 deepseek 价失真）。"""

    def test_model_price_differs_by_model(self, monkeypatch):
        monkeypatch.delenv("SECPT_PRICE_OPENAI_IN_PER_1K", raising=False)
        monkeypatch.delenv("SECPT_PRICE_OPENAI_OUT_PER_1K", raising=False)
        b_ds = BudgetTracker(1.0, model="deepseek-chat")
        b_oa = BudgetTracker(1.0, model="openai/gpt-4o")
        b_ds.add(1000, 1000)
        b_oa.add(1000, 1000)
        # gpt-4o 单价远高于 deepseek-chat：同 token 成本更高、百分比更高
        assert b_oa.cost_cny > b_ds.cost_cny
        assert b_oa.percent() > b_ds.percent()

    def test_provider_env_override(self, monkeypatch):
        monkeypatch.setenv("SECPT_PRICE_OPENAI_IN_PER_1K", "0.5")
        monkeypatch.setenv("SECPT_PRICE_OPENAI_OUT_PER_1K", "1.0")
        b = BudgetTracker(1.0, model="openai/gpt-4o")
        assert b.input_per_1k == 0.5 and b.output_per_1k == 1.0
        b.add(1000, 0)
        assert abs(b.cost_cny - 0.5) < 1e-9

    def test_model_table_exact_match(self, monkeypatch):
        monkeypatch.delenv("SECPT_PRICE_OPENAI_IN_PER_1K", raising=False)
        assert BudgetTracker(1.0, model="openai/gpt-4o-mini").input_per_1k == 0.0011

    def test_unknown_model_falls_back_to_generic(self, monkeypatch):
        monkeypatch.setenv("SECPT_PRICING_INPUT_PER_1K", "0.003")
        monkeypatch.setenv("SECPT_PRICING_OUTPUT_PER_1K", "0.009")
        b = BudgetTracker(1.0, model="mygw/qwen3-32b")
        assert b.input_per_1k == 0.003 and b.output_per_1k == 0.009

    def test_tokens_cost_model_kwarg(self):
        assert abs(tokens_cost(1000, 0, model="openai/gpt-4o") - 0.018) < 1e-9
        # 不传 model：兼容旧口径（deepseek 默认价）
        assert abs(tokens_cost(1000, 0) - 0.002) < 1e-9
