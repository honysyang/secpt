"""E1 L2 视图巩固（WORKFLOW §7 E1 / §9.2-1）：

1. store 层视图函数（pa/store/view.py）：filter_clues 类型过滤 / 窗口
   语义与手工切片全等；clue_summary 按 kind 聚合（计数、样本键 <=5、
   source_tasks 排序去重）。
2. context 接线（pa/core/context.py）：orchestrator fallback 走 L2
   索引（态势轮廓，不铺正文）；payload 显式传 clues 仍走正文窗口
   （尊重调用者，worker 简报链不变）；core/planner 行为不变；
   字符预算截断后尾部提示行不破 CLUE_CHAR_BUDGET+64 断言。
全部渲染确定性（类型序 / 来源序固定，无时间戳 / 随机数）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pa.assets import AssetSnapshot
from pa.core.context import CLUE_CHAR_BUDGET, render_sections
from pa.models import (
    AcceptanceItem,
    Budget,
    Clue,
    MissionSpec,
    MissionState,
)
from pa.store.view import clue_summary, filter_clues

_REPO = Path(__file__).resolve().parent.parent


def _clue(
    kind: str, key: str, source_task: str | None = None, big: str | None = None
) -> Clue:
    return Clue(kind=kind, key=key, value=big or {"k": key}, source_task=source_task)


# ── filter_clues（store 层能力，策略归调用方）──────────────────


def test_filter_clues_no_kinds_equals_manual_slice():
    """类型不传 = 能力全等旧行为（末 window 条、原序）。"""
    clues = [_clue("host", f"h{i}") for i in range(80)]
    assert filter_clues(clues) == clues[-60:]
    assert filter_clues(clues, window=5) == clues[-5:]


def test_filter_clues_kind_filter():
    clues = [
        _clue("host", "h1"),
        _clue("port", "p1"),
        _clue("host", "h2"),
        _clue("cred", "c1"),
    ]
    picked = filter_clues(clues, kinds={"host"})
    assert [c.key for c in picked] == ["h1", "h2"]


def test_filter_clues_empty_and_no_window():
    assert filter_clues([]) == []
    clues = [_clue("host", "h1")]
    # window <=0 = 不开窗口（全量直通）
    assert filter_clues(clues, window=0) == clues


# ── clue_summary（L2 聚合口径统一经 store 视图）────────────────


def test_clue_summary_aggregates_counts_and_sample_keys():
    clues = [
        _clue("host", "h1", source_task="t-0002"),
        _clue("host", "h2", source_task="t-0001"),
        _clue("host", "h1", source_task="t-0002"),
        _clue("port", "p1"),
    ]
    s = clue_summary(clues)
    assert s == {
        "host": {
            "count": 3,
            "keys": ["h1", "h2"],
            "source_tasks": ["t-0001", "t-0002"],
        },
        "port": {"count": 1, "keys": ["p1"], "source_tasks": []},
    }


def test_clue_summary_keys_capped_at_5_in_first_seen_order():
    clues = [_clue("host", f"h{i}") for i in reversed(range(9))]
    s = clue_summary(clues)
    assert s["host"]["keys"] == ["h8", "h7", "h6", "h5", "h4"]
    assert s["host"]["count"] == 9


def test_clue_summary_empty():
    assert clue_summary([]) == {}


# ── context 接线：orchestrator 索引 / 其他 phase 不变 ──────────


@pytest.fixture(scope="module")
def assets():
    return AssetSnapshot.freeze(_REPO / "pa/core/prompts", [_REPO / "assets" / "skills"])


def _state(clues: list[Clue]) -> MissionState:
    spec = MissionSpec(
        objective="评估暴露面",
        targets=["10.0.0.5:80"],
        acceptance=[
            AcceptanceItem(
                id="a1", item="端口状态清单", evidence="port.open", target="10.0.0.5"
            )
        ],
        budget=Budget(),
        allowed_scope=["10.0.0.0/24:80"],
    )
    st = MissionState(spec=spec, mission_id="m1")
    st.clues = list(clues)
    st.clue_seq = len(clues)
    return st


def _role_clues() -> list[Clue]:
    return [
        _clue("host", "h1", source_task="t-0002"),
        _clue("host", "h2", source_task="t-0001"),
        _clue("port", "p1"),
    ]


def test_orchestrator_fallback_renders_index(assets):
    """E1 判据：orchestrator 态势视图 = L2 索引（类型聚合），不铺正文。"""
    st = _state(_role_clues())
    secs = render_sections("orchestrator", st, {"ready": []}, assets)
    text = secs["clues"]
    assert "线索索引（按类型" in text
    assert "host ×2" in text and "port ×1" in text
    assert "t-0001, t-0002" in text
    # 不允许正文线索行出现（L2 ≠ 全量 value 行）
    assert "# 线索（旧→新，可裁剪）" not in text
    assert "[3] port" not in text


def test_orchestrator_index_deterministic(assets):
    """同一状态重复渲染逐字节一致（确定性硬约束）。"""
    st = _state(_role_clues())
    a = render_sections("orchestrator", st, {"ready": []}, assets)["clues"]
    b = render_sections("orchestrator", st, {"ready": []}, assets)["clues"]
    assert a == b


def test_orchestrator_empty_clues_renders_index_header(assets):
    """空线索不输出废弃正文，只留索引头（可观测、空而不炸）。"""
    st = _state([])
    text = render_sections("orchestrator", st, {}, assets)["clues"]
    assert "线索索引（按类型" in text
    assert "×" not in text


def test_payload_clues_still_body_render(assets):
    """payload 显式传 clues = 调用者定了策略，走正文窗口不变。"""
    st = _state(_role_clues())
    secs = render_sections("orchestrator", st, {"clues": st.clues}, assets)
    assert "# 线索（旧→新，可裁剪）" in secs["clues"]
    assert "- [3] port" in secs["clues"]


def test_core_and_planner_fallback_unchanged(assets):
    """core 仍看正文窗口（全量 seq 末窗）；planner 引导阶段空。"""
    st = _state(_role_clues())
    core = render_sections("core", st, {"step": 1}, assets)["clues"]
    assert "# 线索（旧→新，可裁剪）" in core
    assert "- [3] port" in core
    planner = render_sections("planner", st, {}, assets)["clues"]
    assert planner == "# 线索（旧→新，可裁剪）"


def test_char_budget_drop_hint_stays_in_margin(assets):
    """字符预算截断后尾部提示行去哪查全集，且总长不破
    CLUE_CHAR_BUDGET+64（test_context 的逐段上限断言口径）。"""
    clues = [_clue("host", f"big{i}", big="x" * 2000) for i in range(60)]
    st = _state(clues)
    secs = render_sections("core", st, {"step": 1}, assets)
    text = secs["clues"]
    assert "完整清单查线索账本/trace" in text
    assert "条超字符预算" in text
    assert len(text) <= CLUE_CHAR_BUDGET + 64


def test_no_budget_overflow_keeps_no_hint(assets):
    """没截断就别打扰读者（提示行只在真的砍了内容时出现）。"""
    st = _state(_role_clues())
    text = render_sections("core", st, {"step": 1}, assets)["clues"]
    assert "条超字符预算" not in text


def test_render_clues_kind_filter_capability(assets):
    """kinds 能力透传（策略归调用方）：过滤后仍走同一个渲染管线。"""
    from pa.core.context import _render_clues

    clues = _role_clues()
    text = _render_clues(clues, kinds={"port"})
    assert "port" in text
    assert "host" not in text
    # 编号沿用「截断后行号」语义（幂等、确定性）
    assert text.count("\n- ") == 1
