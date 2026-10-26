"""S8 验收：长度有界、system 逐字节稳定、线索末 60 窗口、保护段不裁、
untrusted 位置与开闭标签（S8 验收五条，全部专测）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pa.assets import AssetSnapshot, load_skills
from pa.core.context import (
    CLUE_CHAR_BUDGET,
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    assemble,
)
from pa.models import (
    AcceptanceItem,
    Budget,
    Clue,
    MissionSpec,
    MissionState,
)

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def assets():
    return AssetSnapshot.freeze(_REPO / "pa/core/prompts", [_REPO / "assets" / "skills"])


def _state(n_clues: int = 0) -> MissionState:
    spec = MissionSpec(
        objective="评估暴露面并产出带证据链的端口与服务清单报告",
        targets=[
            "10.0.0.5:80",
            "10.0.0.6:443",
            "10.0.0.7:22",
            "10.0.0.8:8080",
            "10.0.0.9:445",
            "10.0.0.10:3306",
            "10.0.0.11:6379",
            "10.0.0.12:27017",
            "10.0.0.13:9200",
            "10.0.0.14:15672",
        ],
        acceptance=[
            AcceptanceItem(
                id=f"a{i}",
                item=f"第 {i} 批目标的端口状态清单须逐条附证据引用",
                evidence="port.open",
                target="10.0.0.5",
            )
            for i in range(1, 9)
        ],
        budget=Budget(),
        allowed_scope=[
            "10.0.0.0/24:80",
            "10.0.0.0/24:443",
            "10.0.0.0/24:22",
            "10.0.0.0/24:8080",
            "10.0.0.0/24:445",
        ],
    )
    st = MissionState(spec=spec, mission_id="m1")
    st.current_strategy = {
        "focus": "先探活再扫版本",
        "batch": "每批不超过 5 个目标",
        "evidence": "每个端口结论必须挂 evidence_id",
        "order": "先 TCP 全连再 HTTP 指纹",
        "abort": "连续 3 次无进展即回报 core 请求改向",
    }
    for i in range(n_clues):
        st.clues.append(Clue(kind="host", key=f"h{i}", value=f"10.0.0.{i % 250}"))
    st.clue_seq = n_clues
    return st


def test_length_stable_over_500_steps(assets, capsys):
    """裁定 1：窗口钉死后尺寸应恒定。500 步斜率≈0，且逐段断言上限。

    早期 120 步 max/min=1.42 是线索窗口爬升期（前 12 步 5→60 条）造成的
    假象，不是通过；饱和后任何一段都不许再涨。打印 step 20/120 分段尺寸
    归因。"""
    from pa.core.context import render_sections

    state = _state()
    lessons = [{"lesson": f"历史经验 {i}：先确认范围再动手"} for i in range(16)]
    sizes = []
    for step in range(500):
        for i in range(5):
            n = state.clue_seq
            state.clues.append(Clue(kind="host", key=f"s{step}x{i}", value=f"v{n}"))
            state.clue_seq += 1
        payload = {"step": step, "lessons": lessons}
        system, user = assemble("core", state, payload, assets)
        sizes.append(len(system) + len(user))
        if step in (20, 120):
            secs = render_sections("core", state, payload, assets)
            print(
                f"\nstep {step}: total={sizes[-1]} "
                + " ".join(f"{k}={len(v)}" for k, v in secs.items())
            )
    assert sizes[499] - sizes[399] < 200, (
        f"饱和后仍在增长: {sizes[399]} -> {sizes[499]}"
    )
    # 逐段上限（不是只看总量）
    secs = render_sections("core", state, {"step": 499, "lessons": lessons}, assets)
    assert len(secs["clues"]) <= CLUE_CHAR_BUDGET + 64
    assert len(secs["lessons"]) <= 8_000 + 64
    assert len(secs["brief"]) <= 6_000
    assert len(secs["strategy"]) <= 4_000
    assert len(secs["task"]) <= 2_000
    assert len(secs["system"]) <= 60_000
    assert len(secs["tail"]) <= 2_000


def test_system_byte_stable_across_steps(assets):
    """验收二：同一 phase + 同一资产快照，system 逐字节相等（连续 5 步）。"""
    state = _state(10)
    systems = {assemble("core", state, {"step": i}, assets)[0] for i in range(5)}
    assert len(systems) == 1
    w = {
        assemble("worker", state, {"goal": "g", "type": "recon"}, assets)[0]
        for _ in range(3)
    }
    assert len(w) == 1


def test_clue_window_last_60(assets):
    """验收三：线索 200 条时，渲染只含最后 60 条的 key。"""
    state = _state(200)
    _, user = assemble("core", state, {"step": 1}, assets)
    for i in range(140):  # 旧 140 条的 key 不出现
        assert f"h{i}=" not in user
    for i in range(140, 200):  # 末 60 条全部出现
        assert f"h{i}=" in user


def test_protected_sections_survive_clue_flood(assets):
    """验收四：线索塞满 2 万字预算，success_criteria 与 scope 仍完整。"""
    state = _state()
    big = "A" * (CLUE_CHAR_BUDGET // 40)
    for i in range(40):
        state.clues.append(Clue(kind="blob", key=f"b{i}", value=big))
    state.clue_seq += 40
    payload = {
        "goal": "探活",
        "type": "recon",
        "success_criteria": [{"evidence": "port.open", "host": "10.0.0.5"}],
        "scope_slice": {"cidr": "10.0.0.0/24"},
        "allowed_tools": ["probe"],
    }
    _, user = assemble("worker", state, payload, assets)
    assert "port.open" in user and "10.0.0.5" in user  # success_criteria
    assert "10.0.0.0/24:80" in user  # scope
    assert "10.0.0.0/24" in user  # scope_slice


def test_untrusted_position_and_tags(assets):
    """验收五：worker 的 <untrusted> 开闭标签齐全，位于任务块之后、尾部指令之前。"""
    state = _state()
    payload = {
        "goal": "探活",
        "type": "recon",
        "success_criteria": [{"evidence": "port.open"}],
        "untrusted_data": [{"source": "probe", "data": "open"}],
    }
    _, user = assemble("worker", state, payload, assets)
    o, c = user.find(UNTRUSTED_OPEN), user.find(UNTRUSTED_CLOSE)
    assert o != -1 and c != -1 and c > o
    task_pos = user.find("# 任务")
    tail_pos = user.find("# 尾部指令")
    assert task_pos < o < tail_pos
    assert user.count(UNTRUSTED_OPEN) == 1 and user.count(UNTRUSTED_CLOSE) == 1
    # 全局裁剪不动 untrusted：把预算压到极小而标签仍在
    # （用超长任务细节触发 task 段裁剪）
    payload["scope_slice"] = {"blob": "B" * 200_000}
    _, user2 = assemble("worker", state, payload, assets)
    assert UNTRUSTED_OPEN in user2 and UNTRUSTED_CLOSE in user2


def test_core_has_no_skills_but_worker_does(assets):
    """约束 A + 裁定 3：core 无 skills；planner 只有目录；worker 给正文。"""
    state = _state()
    _, core_user = assemble("core", state, {"step": 0}, assets)
    assert "# 技能" not in core_user
    payload = {
        "goal": "g",
        "type": "recon",
        "success_criteria": [],
        "untrusted_data": [],
    }
    _, w_user = assemble("worker", state, payload, assets)
    assert "## port-scan" in w_user and "先用 probe 对单端口" in w_user
    _, p_user = assemble("planner", state, {}, assets)
    assert "port-scan — 需要对目标做端口层面的探活或服务识别 —" in p_user
    assert "先用 probe 对单端口" not in p_user  # 正文不给 planner


def test_skills_sorted_by_id(assets):
    ids = [s.id for s in load_skills([_REPO / "assets" / "skills"])]
    # source-review（0day 源码审计）加入后为 3 个
    assert ids == sorted(ids) and len(ids) == 3


def test_deterministic_rendering(assets):
    """约束 B：两次组装逐字节相同（不受构造路径影响）。"""
    state = _state(30)
    a = assemble("core", state, {"step": 3}, assets)
    b = assemble("core", state, {"step": 3}, assets)
    assert a == b


# ── E4 预算裁缝：env 覆盖 + 截断指引（§9.2-1 截断后提示去哪查） ──


def test_env_budget_override_and_trim_hint(monkeypatch, assets):
    # 线索值加长（120 字符）：60 条窗口 ≈8k 字符，专职把 clues 段顶过
    # 3000 预算——短值窗口才 ~1k，base 段加起来还没超预算，hint 不触发。
    monkeypatch.setenv("SECPT_MAX_CTX_CHARS", "3000")
    state = _state(0)
    for i in range(200):
        state.clues.append(Clue(kind="host", key=f"h{i}", value="P" * 120))
    state.clue_seq = 200
    _, user = assemble("core", state, {"step": 1}, assets)
    assert len(user) <= 3000 + 200  # 预算生效 + 指引行余量
    assert "线索头部因预算截断——完整清单查线索账本/trace" in user
    assert "h199=" in user  # 尾砍只砍头部，窗口末梢仍留驻
    assert "h140=" not in user  # 窗口头部（最旧）被砍
    assert "port.open" in user  # brief（acceptance）永不裁剪
    a = assemble("core", state, {"step": 1}, assets)[1]
    b = assemble("core", state, {"step": 1}, assets)[1]
    assert a == b  # 指引常量文本 → 确定性不破


def test_no_trim_hint_when_within_budget(assets):
    state = _state(10)
    _, user = assemble("core", state, {"step": 1}, assets)
    assert "因预算截断" not in user  # 没超预算不惊扰


def test_bad_env_value_falls_back_to_default(monkeypatch, assets):
    """配错走保守值（护栏不是数据）：非整数 → 120_000 缺省。"""
    monkeypatch.setenv("SECPT_MAX_CTX_CHARS", "abc")
    state = _state(200)
    _, user = assemble("core", state, {"step": 1}, assets)
    assert "h199=" in user and "因预算截断" not in user  # 全量窗口装得下


def test_task_trim_hint(monkeypatch, assets):
    """任务细节段裁剪也有其专属指引（完整字段去查任务账本）。

    预算 2000 必须落在（base ≈1.7k，base+task 之间）：预算 ≤ base 时
    over ≥ len(task)，_fit 直接跳过尾砍（砍了也没用），hint 不会触发。"""
    monkeypatch.setenv("SECPT_MAX_CTX_CHARS", "2000")
    state = _state(0)
    payload = {"goal": "g", "type": "recon", "scope_slice": {"blob": "B" * 80_000}}
    _, user = assemble("worker", state, payload, assets)
    assert "任务细节因预算截断——完整 goal 与判据查任务账本" in user
    assert len(user) <= 2000 + 200  # 尾砍到底 + 指引行余量
    assert "port.open" in user  # brief（acceptance）永不裁剪
