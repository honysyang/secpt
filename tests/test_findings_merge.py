"""重复 finding 合并 + origin 溯源边（WORKFLOW §9.2-3 / §8.3-4）。

- 合并语义（§9.2-3）：下沉到台账投影——同 (asset_id, fingerprint) 提案
  取最新内容、severity 降杆（防重复提案刷高严重度），不再逐条堆叠；
  账本保持 append-only 的原始提案流，合并只作用于内存投影；
- origin（§8.3-4）：提案带产出它的 task id，合并做并集——出口审计（M3）
  据此回答「谁出的这条」。老账本无 origin 字段，默认空，回放兼容。
"""

from __future__ import annotations

from pa.adjudicate import finding_id
from pa.models import (
    AcceptanceItem,
    Budget,
    Event,
    FindingProposal,
    MissionSpec,
    MissionState,
)
from pa.store.derive import finding_fingerprint, merge_finding
from pa.store.event import apply_event
from pa.store.view import StoreView
from pa.store.writer import StoreWriter

import pytest

FINGERPRINT_LEN = 16


def _spec() -> MissionSpec:
    return MissionSpec(
        objective="o",
        targets=["10.0.0.5:443"],
        acceptance=[
            AcceptanceItem(id="a1", item="tls", evidence="tls.cert", target="10.0.0.5")
        ],
        budget=Budget(),
        allowed_scope=["10.0.0.0/24:443"],
    )


def _state() -> MissionState:
    return MissionState(spec=_spec(), mission_id="t")


def _f(
    claim: str = "/.git/ 目录可列出",
    sev: str = "high",
    asset: str = "10.0.0.5",
    refs: list[str] | None = None,
    origin: list[str] | None = None,
) -> FindingProposal:
    return FindingProposal(
        claim=claim,
        severity=sev,  # severity 由 pydantic Literal 校验
        evidence_refs=refs if refs is not None else ["ev-1"],
        reproduce="curl http://10.0.0.5/.git/",
        falsify_if=["403 或 404 稳定出现"],
        asset_id=asset,
        origin=origin if origin is not None else ["t-0001"],
    )


def _fev(seq: int, f: FindingProposal, tid: str = "t-0001") -> Event:
    return Event(
        seq=seq,
        mission_id="t",
        type="finding.proposed",
        actor=f"worker:{tid}",
        payload={"finding": f.model_dump(mode="json")},
        idem_key=f"find:{tid}:{finding_id(f)}",
    )


def _apply(state, *evs: Event) -> None:
    for ev in evs:
        apply_event(state, ev)


def test_offer_fingerprint_stable_across_evolution() -> None:
    """evidence/severity/origin 是演化字段，不动指纹；claim/repro/asset 变才变。"""
    base = _f()
    assert len(finding_fingerprint(base)) == FINGERPRINT_LEN
    same_id = finding_fingerprint(
        base.model_copy(
            update={"evidence_refs": ["ev-9"], "severity": "info", "origin": ["t-x"]}
        )
    )
    assert same_id == finding_fingerprint(base)
    assert finding_fingerprint(
        base.model_copy(update={"claim": "不同断言"})
    ) != finding_fingerprint(base)
    assert finding_fingerprint(
        base.model_copy(update={"asset_id": "10.0.0.6"})
    ) != finding_fingerprint(base)
    assert finding_fingerprint(
        base.model_copy(update={"reproduce": "换一条复现路径"})
    ) != finding_fingerprint(base)


def test_same_fingerprint_merges_latest_wins() -> None:
    """同 (asset+fingerprint) 二提：合并为一条，内容取最新，origin 并集。"""
    state = _state()
    _apply(
        state,
        _fev(1, _f(sev="high", refs=["ev-1"], origin=["t-0001"])),
        _fev(2, _f(sev="info", refs=["ev-2"], origin=["t-0002"]), tid="t-0002"),
    )
    assert len(state.findings) == 1
    merged = state.findings[0]
    assert merged.evidence_refs == ["ev-2"]  # 取最新
    assert merged.claim == "/.git/ 目录可列出"
    assert merged.origin == ["t-0001", "t-0002"]  # 溯源并集（排序稳定）
    fid = finding_id(merged)
    assert isinstance(fid, str) and len(fid) == 12  # 内容哈希 id 照常可用


def test_merge_takes_lower_severity() -> None:
    """降杆：合并 severity 取更保守者——不管新提案多高。"""
    state = _state()
    _apply(state, _fev(1, _f(sev="high")), _fev(2, _f(sev="info")))
    assert state.findings[0].severity == "info"
    state2 = _state()
    _apply(state2, _fev(1, _f(sev="low")), _fev(2, _f(sev="critical")))
    assert state2.findings[0].severity == "low"


def test_different_claim_or_asset_keeps_separate() -> None:
    """异 claim 或异 asset：不合并，台账保持独立条目。"""
    state = _state()
    _apply(
        state,
        _fev(1, _f(claim="A 断言", asset="10.0.0.5")),
        _fev(2, _f(claim="B 断言", asset="10.0.0.5")),
        _fev(3, _f(claim="A 断言", asset="10.0.0.6")),
    )
    assert len(state.findings) == 3


def test_apply_project_is_idempotent() -> None:
    """同一提案事件重复 apply（重放/续跑）结果不变：合并到自身是恒等。"""
    state = _state()
    ev = _fev(1, _f(origin=["t-0001"]))
    _apply(state, ev)
    before = state.findings[0].model_dump(mode="json")
    _apply(state, ev)  # 再放一次
    assert state.findings[0].model_dump(mode="json") == before
    assert len(state.findings) == 1


def test_replay_and_live_converge(tmp_mission) -> None:
    """断点续跑一致性：真实账本两条不同提案 → restore 重放收敛为一条合并。"""
    (tmp_mission / "mission.json").write_text(
        _spec().model_dump_json(), encoding="utf-8"
    )
    w = StoreWriter(tmp_mission)
    w.append(
        "finding.proposed",
        "worker:t-0001",
        {
            "finding": _f(sev="high", refs=["ev-1"], origin=["t-0001"]).model_dump(
                mode="json"
            )
        },
        "find:t-0001:a",
    )
    w.append(
        "finding.proposed",
        "worker:t-0002",
        {
            "finding": _f(sev="low", refs=["ev-2"], origin=["t-0002"]).model_dump(
                mode="json"
            )
        },
        "find:t-0002:b",
    )
    live = StoreView(tmp_mission).restore()  # 全量重放
    assert len(live.findings) == 1
    assert live.findings[0].severity == "low"  # 降杆
    assert live.findings[0].evidence_refs == ["ev-2"]  # 最新
    assert live.findings[0].origin == ["t-0001", "t-0002"]
    # 账本 append-only：两条原始提案都保留（trace 取证口径）
    lines = (tmp_mission / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_old_ledger_without_origin_replays(tmp_mission) -> None:
    """老账本兼容：payload 无 origin 字段，回放不炸、origin 默认空。"""
    (tmp_mission / "mission.json").write_text(
        _spec().model_dump_json(), encoding="utf-8"
    )
    w = StoreWriter(tmp_mission)
    legacy = _f().model_dump(mode="json")
    del legacy["origin"]  # 模拟老账本
    w.append("finding.proposed", "worker:t-0001", {"finding": legacy}, "find:old:1")
    state = StoreView(tmp_mission).restore()
    assert len(state.findings) == 1
    assert state.findings[0].origin == []
    # 老提案再提一次（新代码提案会带 origin）：合并仍成立，origin 并集为单条
    merge_finding(state, _f(origin=["t-0001"]))
    assert len(state.findings) == 1
    assert state.findings[0].origin == ["t-0001"]


@pytest.mark.parametrize("sev", ["info", "low", "med", "high", "critical"])
def test_severity_enum_untouched(sev: str) -> None:
    """severity 枚举五值原样可用（老 payload 回归口径）。"""
    state = _state()
    _apply(state, _fev(1, _f(sev=sev)))
    assert state.findings[0].severity == sev
