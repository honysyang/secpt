"""E3 长期经验库（WORKFLOW §7 b10 / §9.2-3 只读继承纪律）：

1. library_path：SECPT_EXPERIENCES 显式覆盖 + 工作区级缺省；
2. load 容错：坏行 / 非 dict / 缺 lesson 逐行跳过 + 告警，好条目保序；
3. lessons_for 类型过滤：applies_to 与任务类型精确匹配，空 = 通用条目；
4. distill：LLM 成功追加（origin 幂等），三态降级（llm=None / 未注入
   phase / parse 失败）一律 False + 告警，绝不阻塞收口；
5. 注入接线只读：planner 引导（全库）与 compile_briefing（类型过滤）
   空库/未命中时旧行为不变，库文件在注入侧零写路径；
6. loop._finish 收口钩子端到端：exit_audit 之后、mission.finished 之前
   调用 distill，MockLLM distill 脚本入库带 origin/inherited。
"""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

import pytest

from pa import envs
from pa.assets import AssetSnapshot
from pa.core.delegate import compile_briefing
from pa.core.experience import distill, library_path, lessons_for, load
from pa.core.planner import bootstrap_plan
from pa.infra.llm import MockLLM
from pa.models import (
    AcceptanceItem,
    Budget,
    MissionSpec,
    MissionState,
    Task,
)
from pa.runtime.loop import _finish
from pa.store.writer import StoreWriter

ROOT = Path(__file__).resolve().parent.parent
_PROMPTS = ROOT / "pa" / "core" / "prompts"


@pytest.fixture(autouse=True)
def isolated_library(tmp_path, monkeypatch):
    """经验库路径钉到本测试 tmp_path 内（conftest tmp_mission 的父目录
    missions/.test 跨测试共享——任何依赖缺省「父级路径」的写都会互相
    污染；SECPT_EXPERIENCES 覆盖是生产同路径，不算测试专用后门）。"""
    lib = tmp_path / "experiences.jsonl"
    monkeypatch.setenv(envs.EXPERIENCES, str(lib))
    yield lib


@pytest.fixture(scope="module")
def assets():
    return AssetSnapshot.freeze(_PROMPTS, [ROOT / "assets" / "skills"])


def _seed(lib, *entries):
    with lib.open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


_ENTRY = {"applies_to": ["recon"], "lesson": "recon 先确认监听端口", "inherited": True}


def _spec() -> MissionSpec:
    return MissionSpec(
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


def _state(lib) -> MissionState:
    return MissionState(spec=_spec(), mission_id=lib.name)


# ── library_path：env 覆盖 + 缺省 ────────────────────────────


def test_library_path_override(tmp_path):
    # autouse fixture 已设 env → 覆盖优先于任何 root 锚点
    assert library_path(tmp_path) == tmp_path / "experiences.jsonl"


def test_library_path_default_is_workspace_level(tmp_path, monkeypatch):
    monkeypatch.delenv(envs.EXPERIENCES, raising=False)
    assert library_path(tmp_path) == tmp_path.parent / "experiences.jsonl"


# ── load：容错与保序 ────────────────────────────────────────


def test_load_missing_file_is_empty(tmp_path):
    assert load(tmp_path / "experiences.jsonl") == []


def test_load_tolerates_bad_lines_keeps_order(tmp_path, caplog):
    lib = tmp_path / "experiences.jsonl"
    _seed(
        lib,
        {"lesson": "第一条", "applies_to": []},
        {"lesson": "", "applies_to": []},  # 缺正文 → 跳过
        ["not", "a", "dict"],  # 非 dict → 跳过
        {"lesson": "第二条"},
    )
    with caplog.at_level(logging.WARNING, logger="secpt.experience"):
        lines = lib.read_text(encoding="utf-8").splitlines()
        lines[2] = "{not json"  # 非 dict 行改写为坏 JSON（真实坏行同源）
    lib.write_text("\n".join(lines), encoding="utf-8")
    entries = load(lib)
    assert [e["lesson"] for e in entries] == ["第一条", "第二条"]
    assert "不是合法 JSON" in caplog.text
    assert "缺 lesson 正文" in caplog.text


def test_load_all_bad_lines_is_empty(tmp_path, caplog):
    lib = tmp_path / "experiences.jsonl"
    lib.write_text("{bad\n\n[1]\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="secpt.experience"):
        assert load(lib) == []


# ── lessons_for：类型过滤 + 通用条目 ─────────────────────────


def test_lessons_for_filters_by_type(tmp_path):
    lib = tmp_path / "experiences.jsonl"
    _seed(
        lib,
        {"applies_to": ["recon"], "lesson": "recon 条目"},
        {"applies_to": ["web"], "lesson": "web 条目"},
        {"applies_to": [], "lesson": "通用条目"},
    )
    hits = lessons_for(tmp_path, ["recon"])
    assert [e["lesson"] for e in hits] == ["recon 条目", "通用条目"]


def test_lessons_for_generic_entry_always_injected(tmp_path):
    lib = tmp_path / "experiences.jsonl"
    _seed(lib, {"lesson": "通用条目"})
    assert [e["lesson"] for e in lessons_for(tmp_path, ["web"])] == ["通用条目"]
    assert lessons_for(tmp_path, []) == [{"lesson": "通用条目"}]


# ── distill：成功追加 + 幂等 + 三态降级 ───────────────────────


def _distill_llm(lesson: str = "后面先确认监听端口再上 Web 攻面") -> MockLLM:
    return MockLLM(
        {
            "distill": [
                json.dumps(
                    {"lessons": [{"applies_to": ["recon"], "lesson": lesson}]},
                    ensure_ascii=False,
                )
            ]
        }
    )


def test_distill_appends_with_origin_and_inherited(tmp_path):
    lib = tmp_path / "experiences.jsonl"
    st = _state(lib)
    assert distill(st, tmp_path, _distill_llm(), finish_reason="completed") is True
    entries = load(lib)
    assert len(entries) == 1
    assert entries[0]["origin"] == st.mission_id
    assert entries[0]["inherited"] is True
    assert entries[0]["applies_to"] == ["recon"]
    assert entries[0]["finish_reason"] == "completed"
    assert entries[0]["tasks_done"] == 0
    assert entries[0]["clues"] == 0


def test_distill_idempotent_on_same_origin(tmp_path):
    lib = tmp_path / "experiences.jsonl"
    st = _state(lib)
    llm = _distill_llm()
    distill(st, tmp_path, llm, finish_reason="completed")
    before = lib.read_bytes()
    # 幂等键在 LLM 之前判断——即使调用会失败也已短路（MockLLM 空队列）
    assert distill(st, tmp_path, MockLLM(), finish_reason="completed") is False
    assert lib.read_bytes() == before


def test_distill_degrades_when_phase_not_injected(tmp_path, caplog):
    lib = tmp_path / "experiences.jsonl"
    st = _state(lib)
    with caplog.at_level(logging.WARNING, logger="secpt.experience"):
        assert distill(st, tmp_path, MockLLM(), finish_reason="completed") is False
    assert not lib.exists()  # 无条目落库
    assert "经验提炼失败" in caplog.text


def test_distill_degrades_on_parse_error(tmp_path, caplog):
    lib = tmp_path / "experiences.jsonl"
    st = _state(lib)
    llm = MockLLM({"distill": ["{not json"]})
    with caplog.at_level(logging.WARNING, logger="secpt.experience"):
        assert distill(st, tmp_path, llm, finish_reason="plateau") is False
    assert not lib.exists()
    assert "经验提炼失败" in caplog.text


def test_distill_none_llm_is_noop(tmp_path):
    lib = tmp_path / "experiences.jsonl"
    st = _state(lib)
    assert distill(st, tmp_path, None, finish_reason="completed") is False
    assert not lib.exists()


def test_distill_input_carries_mechanical_signals(tmp_path):
    """LLM user prompt 是机械信号快照：目标 / 结束原因 / 验收 / 审计行。"""
    lib = tmp_path / "experiences.jsonl"
    st = _state(lib)
    llm = _distill_llm()
    distill(
        st,
        tmp_path,
        llm,
        finish_reason="completed",
        audit_summary={"refs": 3, "verified": 2, "broken": ["f-1", "f-2"]},
    )
    (call,) = [c for c in llm.calls if c["phase"] == "distill"]
    assert "评估暴露面" in call["user"]
    assert "结束原因: completed" in call["user"]
    assert "0/1 项满足" in call["user"]
    assert "断链 2" in call["user"]


def test_distill_input_counts_broken_dicts(tmp_path):
    """audit_refs 的 broken 是 dict 列表（真跑形态），不能再按 str 数成 0。"""
    lib = tmp_path / "experiences.jsonl"
    st = _state(lib)
    llm = _distill_llm()
    distill(
        st,
        tmp_path,
        llm,
        finish_reason="plateau",
        audit_summary={
            "refs": 1,
            "verified": 0,
            "broken": [{"finding": "f-1", "claim": "x", "refs": ["ev-0"]}],
        },
    )
    (call,) = [c for c in llm.calls if c["phase"] == "distill"]
    assert "断链 1" in call["user"]


# ── 注入接线（只读） ─────────────────────────────────────────


def test_bootstrap_plan_injects_all_lessons_readonly(tmp_path, assets):
    lib = tmp_path / "experiences.jsonl"
    _seed(lib, dict(_ENTRY))
    before = lib.read_bytes()
    st = MissionState(spec=_spec(), mission_id=tmp_path.name)
    writer = StoreWriter(tmp_path)
    llm = MockLLM(
        {
            "planner": [
                json.dumps(
                    {
                        "tasks": [
                            {
                                "type": "recon",
                                "goal": "probe",
                                "success_criteria": [{"evidence": "probe:1"}],
                                "scope_slice": {},
                                "allowed_tools": ["probe"],
                            }
                        ],
                        "current_strategy": {},
                        "rationale": "r",
                    }
                )
            ]
        }
    )
    bootstrap_plan(st, writer, llm, assets)
    (call,) = [c for c in llm.calls if c["phase"] == "planner"]
    assert "recon 先确认监听端口" in call["user"]
    assert lib.read_bytes() == before  # 注入侧零写路径（L249）


def test_bootstrap_plan_empty_library_no_change(tmp_path, assets):
    st = MissionState(spec=_spec(), mission_id=tmp_path.name)
    writer = StoreWriter(tmp_path)
    llm = MockLLM(
        {
            "planner": [
                json.dumps({"tasks": [], "current_strategy": {}, "rationale": ""})
            ]
        }
    )
    bootstrap_plan(st, writer, llm, assets)  # 空库不炸：plook 库缺省也为空 payload


def _task(tid: str, ttype: str) -> Task:
    return Task(
        id=tid,
        mission_id="m1",
        type=ttype,
        goal=f"{tid}-goal",
        success_criteria=[],
        allowed_tools=["probe"],
    )


def test_compile_briefing_filters_lessons_by_task_type(tmp_path, assets):
    lib = tmp_path / "experiences.jsonl"
    _seed(
        lib,
        {"applies_to": ["recon"], "lesson": "recon 条目"},
        {"applies_to": ["web"], "lesson": "web 条目"},
        {"lesson": "通用条目"},
    )
    st = MissionState(spec=_spec(), mission_id="m1")
    brief = compile_briefing(st, _task("t-0001", "recon"), assets, root=tmp_path)
    assert [e["lesson"] for e in brief["lessons"]] == ["recon 条目", "通用条目"]
    web = compile_briefing(st, _task("t-0002", "web"), assets, root=tmp_path)
    assert [e["lesson"] for e in web["lessons"]] == ["web 条目", "通用条目"]


def test_compile_briefing_root_none_keeps_old_behavior(tmp_path, assets):
    st = MissionState(spec=_spec(), mission_id="m1")
    _seed(tmp_path / "experiences.jsonl", dict(_ENTRY))
    brief = compile_briefing(st, _task("t-0001", "recon"), assets)  # root 缺省
    assert brief["lessons"] == []


# ── loop._finish 收口钩子（端到端） ───────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_finish_runs_distill_hook_between_audit_and_finished(tmp_path):
    lib = tmp_path / "experiences.jsonl"
    st = MissionState(spec=_spec(), mission_id=tmp_path.name)
    writer = StoreWriter(tmp_path)
    llm = _distill_llm()
    _finish(st, writer, "completed", llm=llm)
    assert st.finished
    entries = load(lib)
    assert entries[0]["origin"] == tmp_path.name
    events = writer.events_path.read_text(encoding="utf-8").splitlines()
    types = [json.loads(ln)["type"] for ln in events]
    assert types.index("mission.audit") < types.index(
        "mission.finished"
    )  # distill 夹在两者之间（审计在先、收口在后）
    (call,) = [c for c in llm.calls if c["phase"] == "distill"]
    assert "结束原因: completed" in call["user"]


def test_run_end_to_end_distills_to_library(tmp_path, assets):
    port = _free_port()
    spec = _spec()
    lib = tmp_path / "experiences.jsonl"
    llm = MockLLM(
        {
            "planner": [
                json.dumps(
                    {
                        "tasks": [
                            {
                                "type": "recon",
                                "goal": f"probe 127.0.0.1:{port}",
                                "success_criteria": [{"evidence": "open"}],
                                "scope_slice": {},
                                "allowed_tools": ["probe"],
                                "budget": {"steps": 4},
                            }
                        ],
                        "current_strategy": {},
                        "rationale": "r",
                    }
                )
            ],
            "worker": [
                json.dumps(
                    {
                        "kind": "call_tool",
                        "tool": "probe",
                        "args": {"target": "127.0.0.1", "port": port},
                        "expect": "端口开放",
                    }
                )
            ],
            "distill": [
                json.dumps(
                    {
                        "lessons": [
                            {
                                "applies_to": ["recon"],
                                "lesson": "先探活后攻面",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            ],
        }
    )
    from pa.runtime import run

    run(tmp_path, spec, llm=llm, assets=assets)
    entries = load(lib)
    assert entries and entries[0]["origin"] == tmp_path.name
    assert entries[0]["lesson"] == "先探活后攻面"
