"""E2 轮末滚动简报（WORKFLOW §7 E2 / §5.2 红线 / §8.3-2 冷区 digest）：

1. reflect_round（pa/core/memory.py）：提炼成功 → memory.reflect 事件 +
   state.brief 投影；LLM 失败 → 静默降级（无事件 + brief 保持原值）；
   幂等键防崩溃重入重复落账；无 brief = orchestrator 上下文旧行为逐字节
   不变（空值不输出红线）。
2. 渲染接线（pa/core/context.py）：orchestrator 独占 round_brief 段
   （lessons 之后、untrusted 之前）；超预算截断 + 指引查账本；core/
   worker/planner 永不带该段。
3. 回放等价：apply_event 投影与 live 路径一致（restore 同一函数）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa.assets import AssetSnapshot
from pa.core.context import ROUND_BRIEF_CHAR_BUDGET, render_sections
from pa.core.memory import reflect_round
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Budget,
    MissionSpec,
    MissionState,
    Task,
)
from pa.store.event import apply_event
from pa.store.writer import StoreWriter

_REPO = Path(__file__).resolve().parent.parent


def _state() -> MissionState:
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
    return MissionState(spec=spec, mission_id="m1")


def _task(tid: str, state: str, attempt: int = 0) -> Task:
    return Task(
        id=tid,
        mission_id="m1",
        type="probe",
        goal=f"{tid}-goal",
        success_criteria=[],
        allowed_tools=["probe"],
        state=state,
        attempt=attempt,
    )


def _llm_script(summary: str = "本轮探测完成，下一步提权。") -> MockLLM:
    return MockLLM({"reflect": [json.dumps({"summary": summary})]})


@pytest.fixture(scope="module")
def assets():
    return AssetSnapshot.freeze(_REPO / "pa/core/prompts", [_REPO / "assets" / "skills"])


# ── reflect_round：成功投影 / 静默降级 / 幂等 ──────────────────


def test_reflect_writes_event_and_projects_brief(tmp_path):
    st = _state()
    w = StoreWriter(tmp_path)
    llm = _llm_script("本轮探索完成。")
    assert reflect_round(st, w, llm) is True
    assert st.brief == {"step": 0, "summary": "本轮探索完成。"}
    # 账本原文独立可查（冷区 digest 前提）：事件不随滚动丢弃
    kinds = [e.type for e in w._idem.values()]
    assert kinds == ["memory.reflect"]


def test_reflect_degrades_silently_on_call_error(tmp_path):
    """§5.2 红线 3：MockLLM 未注入 reflect phase → ModelCallError →
    无事件、无投影、不炸（现有引擎测试不注入 reflect 的回归安全）。"""
    st = _state()
    w = StoreWriter(tmp_path)
    assert reflect_round(st, w, MockLLM()) is False
    assert st.brief == {}
    assert w._idem == {}


def test_reflect_degrades_silently_on_parse_error(tmp_path):
    """畸形 JSON = ModelParseError，与调用失败同走降级路径。"""
    st = _state()
    w = StoreWriter(tmp_path)
    llm = MockLLM({"reflect": ["{not json"]})
    assert reflect_round(st, w, llm) is False
    assert st.brief == {}
    assert w._idem == {}


def test_reflect_degrades_on_summary_over_limit(tmp_path):
    """超 2000 字符 → schema 校验失败 → 降级，超长摘要不进账本（冷区
    digest 第一道闸在 schema）。"""
    st = _state()
    w = StoreWriter(tmp_path)
    llm = _llm_script("x" * 2001)
    assert reflect_round(st, w, llm) is False
    assert st.brief == {}
    assert w._idem == {}


def test_reflect_degrades_on_empty_summary(tmp_path):
    """min_length=1：空摘要同样不过闸。"""
    st = _state()
    w = StoreWriter(tmp_path)
    llm = MockLLM({"reflect": [json.dumps({"summary": ""})]})
    assert reflect_round(st, w, llm) is False
    assert w._idem == {}


def test_reflect_idempotent_on_crash_reentry(tmp_path):
    """崩溃重入（轮内 counts 不变）：同 key 幂等命中 → 第二次不投影、
    不出第二条事件（§5.2 红线 2）。"""
    st = _state()
    w = StoreWriter(tmp_path)
    llm = _llm_script("摘要A")
    assert reflect_round(st, w, llm) is True
    assert reflect_round(st, w, llm) is False
    st2 = _state()
    for ev in [e for e in sorted(w._idem.values(), key=lambda e: e.seq)]:
        apply_event(st2, ev)
    assert st2.brief == {"step": 0, "summary": "摘要A"}


def test_rolling_brief_overwrites(tmp_path):
    """滚动语义：新一轮新简报覆盖旧投影；账本两轮原文都在（永不丢）。"""
    st = _state()
    w = StoreWriter(tmp_path)
    reflect_round(st, w, _llm_script("第一轮印象"))
    st.step = 1
    st.clues = []  # counts 变化，跨轮 idem 不同
    assert reflect_round(st, w, _llm_script("第二轮印象")) is True
    assert st.brief["summary"] == "第二轮印象"
    assert len(w._idem) == 2


# ── 渲染接线：orchestrator 独占 round_brief 段 ────────────────


def test_orchestrator_renders_round_brief(assets):
    st = _state()
    st.brief = {"step": 3, "summary": "本轮探索完成。"}
    secs = render_sections("orchestrator", st, {"ready": []}, assets)
    text = secs["round_brief"]
    assert "# 上轮简报（step 3，滚动覆盖）" in text
    assert "本轮探索完成。" in text


def test_no_brief_renders_no_section(assets):
    """空 brief = 无该段（未配置 reflect / 降级场景旧行为逐字节不变）。"""
    st = _state()
    bare = render_sections("orchestrator", st, {"ready": []}, assets)
    assert "round_brief" not in bare
    st.brief = {"step": 1, "summary": "s"}
    with_brief = render_sections("orchestrator", st, {"ready": []}, assets)
    assert len(with_brief) == len(bare) + 1  # 只多这一段


def test_round_brief_other_phases_never_get_it(assets):
    """只给 orchestrator：core/worker/planner 不受 L3 影响。"""
    st = _state()
    st.brief = {"step": 1, "summary": "s"}
    for phase, payload in (
        ("core", {"step": 1}),
        ("planner", {}),
        ("worker", {"step": 1}),
    ):
        assert "round_brief" not in render_sections(phase, st, payload, assets)


def test_round_brief_truncation_hint_when_over_budget(assets):
    """冷区 digest（§8.3-2）：渲染层预算截断 + 指引查账本原文。"""
    st = _state()
    st.brief = {"step": 2, "summary": "y" * (ROUND_BRIEF_CHAR_BUDGET + 500)}
    text = render_sections("orchestrator", st, {"ready": []}, assets)["round_brief"]
    assert "memory.reflect" in text
    # 正文截到预算位（提示行的 memory.reflect 不计入）：第二行 = 正文
    body = text.splitlines()[1]
    assert body.count("y") == ROUND_BRIEF_CHAR_BUDGET


def test_round_brief_deterministic(assets):
    st = _state()
    st.brief = {"step": 1, "summary": "s"}
    a = render_sections("orchestrator", st, {"ready": []}, assets)["round_brief"]
    b = render_sections("orchestrator", st, {"ready": []}, assets)["round_brief"]
    assert a == b


# ── apply_event 与 live 路径等价（可回放红线）──────────────────


def test_apply_event_reflect_and_replay(tmp_path):
    st = _state()
    w = StoreWriter(tmp_path)
    reflect_round(st, w, _llm_script("回放等价简报"))
    restored = _state()
    for ev in sorted(w._idem.values(), key=lambda e: e.seq):
        apply_event(restored, ev)
    assert restored.brief == st.brief
    assert restored.brief["summary"] == "回放等价简报"


def test_snapshot_signals_deterministic(tmp_path):
    """机械信号摘要进入 LLM user prompt：任务分布 / 异常单列 / 台账计数。"""
    st = _state()
    st.tasks = {
        "t-0001": _task("t-0001", "done"),
        "t-0002": _task("t-0002", "failed", attempt=2),
        "t-0003": _task("t-0003", "blocked"),
    }
    w = StoreWriter(tmp_path)
    llm = _llm_script()
    reflect_round(st, w, llm)
    (call,) = [c for c in llm.calls if c["phase"] == "reflect"]
    user = call["user"]
    assert "t-0002 failed(尝试2)" in user
    assert "t-0003 blocked" in user
    assert "任务状态" in user and "发现台账" in user
