"""pa/adjudicate/rules.py 单测（S11 约束 A：裁决规则独立于调度器，可单测）。

confirmed：≥2 引用、≥1 可核验原始输出、有证伪条件；
false_positive：引用全部无法核验（证据链断裂，写清为什么排除）；
suspected：其余。
"""
from __future__ import annotations

import json

from pa.adjudicate import adjudicate, finding_id
from pa.infra import evidence
from pa.models import FindingProposal
from pa.store.view import StoreView


def _f(refs, falsify=None):
    return FindingProposal(claim="端口开放", severity="low",
                           evidence_refs=refs, reproduce="重跑 probe",
                           falsify_if=falsify if falsify is not None else ["连接被拒绝"])


def _put(tmp_mission, stdout):
    content = json.dumps({"tool": "probe", "exit": 0, "stdout": stdout,
                          "stderr": ""}, ensure_ascii=False).encode("utf-8")
    return evidence.put(tmp_mission, content, task_id="t-0001")


def test_confirmed_two_refs_with_raw_output(tmp_mission):
    e1 = _put(tmp_mission, "open")
    e2 = _put(tmp_mission, "open\nbanner:ok")
    view = StoreView(tmp_mission)
    verdict, reason = adjudicate(view, _f([e1, e2]))
    assert verdict == "confirmed"
    assert "证伪" in reason


def test_suspected_insufficient_evidence(tmp_mission):
    e1 = _put(tmp_mission, "open")
    view = StoreView(tmp_mission)
    verdict, _ = adjudicate(view, _f([e1]))        # 只有 1 条引用
    assert verdict == "suspected"


def test_suspected_no_falsify_condition(tmp_mission):
    e1 = _put(tmp_mission, "open")
    e2 = _put(tmp_mission, "open2")
    view = StoreView(tmp_mission)
    verdict, _ = adjudicate(view, _f([e1, e2], falsify=[]))   # 缺证伪条件
    assert verdict == "suspected"


def test_false_positive_all_refs_broken(tmp_mission):
    bad = ["ev-" + "0" * 16, "ev-" + "f" * 16]
    view = StoreView(tmp_mission)
    verdict, reason = adjudicate(view, _f(bad))
    assert verdict == "false_positive"
    assert "无法核验" in reason                    # 为什么排除必须写清


def test_finding_id_is_content_hash(tmp_mission):
    f1 = _f(["ev-" + "0" * 16])
    f2 = _f(["ev-" + "0" * 16])
    assert finding_id(f1) == finding_id(f2) == finding_id(f1.model_copy())
    assert len(finding_id(f1)) == 12
