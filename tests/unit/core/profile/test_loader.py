"""S4 Profile 加载单测：五个 YAML 全部可加载，字段正确。"""

from __future__ import annotations

import pytest

from secpt.core.profile.loader import CORE_TOOLS, Profile, load_profile

# 五个 profile 的关键期望值（role / terminator kind / budget）
EXPECTED = {
    "ctf": {
        "role": "CTF 解题手",
        "kind": "budget",
        "steps": 100,
        "wall_s": 900.0,
        "prior": "ctf.md",
    },
    "pentest": {
        "role": "渗透测试员",
        "kind": "budget",
        "steps": 300,
        "wall_s": 3600.0,
        "rounds": 20,
        "prior": "pentest.md",
    },
    "redteam": {
        "role": "红队操作员",
        "kind": "budget",
        "wall_s": 28800.0,
        "prior": "redteam.md",
    },
    "assessment": {
        "role": "安全评估员",
        "kind": "budget",
        "wall_s": 28800.0,
        "prior": "assessment.md",
    },
    "codeaudit": {
        "role": "代码审计员",
        "kind": "budget",
        "steps": 400,
        "wall_s": 7200.0,
        "prior": "codeaudit.md",
    },
}


@pytest.mark.parametrize("name,exp", EXPECTED.items())
def test_load_five_profiles(name, exp):
    prof = load_profile(name)
    assert isinstance(prof, Profile)
    assert prof.name == name
    assert prof.role == exp["role"]
    assert prof.terminator.kind == exp["kind"]
    if "flag_regex" in exp:
        assert prof.terminator.flag_regex == exp["flag_regex"]
    assert prof.budget.steps == exp.get("steps", prof.budget.steps)
    assert prof.budget.wall_s == exp["wall_s"]
    assert prof.prior == exp["prior"]
    # 每个 profile 的 prior 文件必须真实存在且非空
    assert prof.prior_path.is_file(), f"prior 文件缺失: {prof.prior_path}"
    assert prof.prior_text()


def test_ctf_terminator():
    prof = load_profile("ctf")
    # flag 样式不固定：不做正则判定，靠模型 ANSWER 自宣告 + budget 收尾
    assert prof.terminator.kind == "budget"
    assert prof.terminator.flag_regex is None
    assert prof.terminator.stop_condition


def test_merged_whitelist_extra_ctf():
    prof = load_profile("ctf")
    extra = prof.merged_whitelist_extra()
    assert "nmap" in extra
    # 内置四工具不进 extra（数据驱动不重复）
    for t in CORE_TOOLS:
        assert t not in extra


def test_merged_whitelist_extra_codeaudit():
    prof = load_profile("codeaudit")
    extra = prof.merged_whitelist_extra()
    assert "semgrep" in extra
    for t in CORE_TOOLS:
        assert t not in extra


def test_unknown_profile_falls_back_default():
    prof = load_profile("no_such_profile")
    assert isinstance(prof, Profile)
    assert prof.name == "no_such_profile"
    assert prof.role == Profile(name="x").role  # 默认角色，不崩
    assert prof.terminator.flag_regex is None  # 默认 terminator 无 flag_regex
