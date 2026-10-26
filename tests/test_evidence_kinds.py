"""证据类型点分枚举的契约测试（补充裁定 3）。

- 每个工具的 extract_clues 产出必须命中它在 TOOL_KIND_CHECKS 登记的
  白名单；
- 构造函数唯一出口：kind 与登记集合同源，调用点无字符串拼装。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa.core.evidence_kinds import (TOOL_KIND_CHECKS, http_kind, probe_kind,
                                    shell_kind, tool_kind_ok)
from pa.core.reflect import extract_clues

_CASES = [
    # (tool, args, exit_code, stdout)
    ("probe", {"target": "10.0.0.5", "port": 80}, 0, b""),
    ("probe", {"target": "10.0.0.5", "port": 81}, 1, b""),
    ("http", {"url": "http://10.0.0.5/"}, 200, b"<html/>"),
    ("http", {"url": "http://10.0.0.5/"}, 404, b"nf"),
    ("http", {"url": "http://10.0.0.5/"}, 1, b""),   # 网络错误约定 exit 1
    ("shell", {"cmd": "whoami"}, 0, b"user=kali\n"),
    ("shell", {"cmd": "nmap x"}, 2, b"scan=partial\n"),
    ("fs", {"op": "read", "path": "a"}, 0, b"content=x\n"),
    ("fs", {"op": "read", "path": "../etc"}, 126, b"blocked=true\n"),
]


@pytest.mark.parametrize("tool,args,exit_code,stdout", _CASES)
def test_clue_kinds_match_tool_whitelist(tool, args, exit_code, stdout):
    out = SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=b"",
                          evidence_id=None, truncated=False,
                          original_bytes=len(stdout), truncated_at=None)
    clues = extract_clues(tool, args, out)
    assert clues, f"{tool} exit={exit_code} 至少应产一条线索"
    for c in clues:
        assert tool_kind_ok(tool, c.kind), \
            f"{tool} 产出未登记 kind: {c.kind}"


def test_kind_constructors_are_the_whitelist_source():
    """登记白名单必须就是构造函数的值域（同一出口，无平行定义）。"""
    assert tool_kind_ok("probe", probe_kind(0))
    assert tool_kind_ok("probe", probe_kind(1))
    assert tool_kind_ok("http", http_kind(200))     # http.status.200
    assert tool_kind_ok("http", http_kind(404))     # http.error
    assert tool_kind_ok("http", http_kind(1))       # http.error（网络错误）
    assert tool_kind_ok("shell", shell_kind(0))     # shell.ok
    assert tool_kind_ok("shell", shell_kind(126))   # shell.error
    assert tool_kind_ok("fs", shell_kind(0))
    for bad in ("port", "path", "tool", "open", "probe.open2", "http.status"):
        assert not any(check(bad) for check in TOOL_KIND_CHECKS.values()), \
            f"旧散名/畸形 kind 不得过白名单: {bad}"


def test_probe_kind_splits_from_constructor_not_literal():
    """value 与 kind 同源：probe 的 open/closed 由构造函数结果派生。"""
    out = SimpleNamespace(exit_code=0, stdout=b"", stderr=b"",
                          evidence_id=None, truncated=False,
                          original_bytes=0, truncated_at=None)
    c = extract_clues("probe", {"target": "h", "port": 1}, out)[0]
    assert c.kind == "probe.open" and c.value == "open"
    out.exit_code = 1
    c = extract_clues("probe", {"target": "h", "port": 1}, out)[0]
    assert c.kind == "probe.closed" and c.value == "closed"


def test_exclusive_groups_any_of_within_family_and_across_families():
    """互斥族内「命中其一即可」，族与族之间仍是 AND。"""
    from pa.core.evidence_kinds import requirement_groups, requirements_met

    # probe.open + probe.closed 同族折叠成一个槽位
    assert requirement_groups(["probe.open", "probe.closed"]) == \
        [["probe.open", "probe.closed"]]
    # 跨族不折叠
    assert requirement_groups(["probe.open", "http.status.200"]) == \
        [["probe.open"], ["http.status.200"]]
    # 同族命中其一即满足
    hit = lambda e: e == "probe.open"
    assert requirements_met(["probe.open", "probe.closed"], hit)
    # 都没有命中 → 不满足
    assert not requirements_met(["probe.open", "probe.closed"], lambda e: False)
    # 跨族要同时命中
    both = lambda e: True
    one = lambda e: e == "probe.open"
    assert requirements_met(["probe.open", "http.status.200"], both)
    assert not requirements_met(["probe.open", "http.status.200"], one)
    # shell 族同样互斥；旧形态字符串不参与分组
    assert requirement_groups(["open", "closed"]) == [["open"], ["closed"]]


def test_criteria_met_treats_exclusive_family_as_any_of():
    """回归真实联调事故：判据 [probe.open, probe.closed] 并列时
    worker 探到 open 即应判据达成，不得 thrashing。"""
    from pa.core.reflect import criteria_met
    from pa.models import Clue, Criterion

    clues = [Clue(kind="probe.open", key="aimohu.com:80", value="open")]
    crit = [Criterion(evidence="probe.open"), Criterion(evidence="probe.closed")]
    assert criteria_met(clues, crit), "同族互斥判据命中其一即可"
    crit2 = [Criterion(evidence="probe.open"),
             Criterion(evidence="http.status.200")]
    assert not criteria_met(clues, crit2), "跨族仍要都命中"
