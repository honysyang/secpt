"""M1 角色分配 + M4 playbook 注入的验收。

硬约束（全部专测）：
1. 未配置角色库/未指派 role 的既有流程：用户段逐字节不变（旧 mission
   兼容——空值不输出）。
2. role 从 TaskDraft → Task → task.created 落账 → 重放还原，全链路一致。
3. 注入确定性：角色按 id、技能双过滤确定，两次组装逐字节相同。
4. 未知 role 静默降级但可见（不拦截任务、不吞错误）。
5. playbook = 数据驱动的技能文件（playbooks/ 目录进 skill_dirs），
   按任务类型注入 worker，复用同一注入机器，零新代码路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa.assets import AssetSnapshot, load_roles
from pa.core.context import assemble, render_sections
from pa.core.delegate import compile_briefing
from pa.errors import PolicyViolation
from pa.graph.task import create_task
from pa.models import (
    AcceptanceItem,
    Budget,
    MissionSpec,
    TaskDraft,
)
from pa.store.view import StoreView
from pa.store.writer import StoreWriter

_REPO = Path(__file__).resolve().parent.parent
_PROMPTS = _REPO / "pa" / "core" / "prompts"


@pytest.fixture
def asset_dirs(tmp_mission):
    """仓库外的临时资产目录（陷阱 A5：运行数据进工作区）。返回三目录。"""
    skills = tmp_mission / "skills"
    playbooks = tmp_mission / "playbooks"
    roles = tmp_mission / "roles"
    for d in (skills, playbooks, roles):
        d.mkdir()
    return skills, playbooks, roles


def _md(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


ROLE_RECON = """---
id: recon
summary: 侦察执行者手册：确认优先、拒绝盲扫
---
只做确认，不盲扫。
"""

SKILL_TAGGED = """---
id: role-only
applies_to: [recon]
roles: [recon]
trigger: t
summary: 角色专属纪律
---
只有 recon 角色能看到我。
"""

PLAYBOOK = """---
id: pb-recon
applies_to: [recon]
trigger: t
summary: recon 方法论阶段引导
---
第一阶段先锚点确认。
"""

DRAFT_PAYLOAD = {
    "goal": "探活 10.0.0.5",
    "type": "recon",
    "success_criteria": [{"evidence": "probe:1"}],
    "scope_slice": {"cidr": "10.0.0.0/24"},
    "allowed_tools": ["probe"],
}


def _freeze(skills: Path, playbooks: Path, roles: Path) -> AssetSnapshot:
    return AssetSnapshot.freeze(_PROMPTS, [skills, playbooks], (roles,))


def _state(tmp_mission) -> tuple:
    spec = MissionSpec(
        objective="评估暴露面",
        targets=["10.0.0.5"],
        acceptance=[
            AcceptanceItem(
                id="a1", item="端口清单", evidence="port.open", target="10.0.0.5"
            )
        ],
        budget=Budget(),
        allowed_scope=["10.0.0.0/24:80"],
    )
    (tmp_mission / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
    return StoreView(tmp_mission).restore(), StoreWriter(tmp_mission)


# ── 加载器（与 skills 同构：排序、去重、容错）──────────────────


def test_load_roles_sorted_and_deduped(asset_dirs):
    skills, playbooks, roles = asset_dirs
    _md(roles / "b-role.md", "---\nid: zulu\nsummary: z\n---\nbody")
    _md(roles / "a-role.md", "---\nid: alpha\nsummary: a\n---\nbody")
    loaded = load_roles([roles])
    assert [r.id for r in loaded] == ["alpha", "zulu"]  # 不依赖文件名
    _md(playbooks / "dup.md", "---\nid: zulu\nsummary: dup\n---\nbody")
    with pytest.raises(PolicyViolation):
        load_roles([roles, playbooks])


def test_role_fields_and_missing_dir(asset_dirs):
    skills, playbooks, roles = asset_dirs
    _md(roles / "recon.md", ROLE_RECON)
    # 不存在的目录整条跳过（与 load_skills 同容错）
    loaded = load_roles([roles, skills / "nope"])
    assert len(loaded) == 1
    r = loaded[0]
    assert r.id == "recon" and "确认" in r.summary and "不盲扫" in r.body


# ── 旧 mission 兼容：空值不输出，逐字节不变 ─────────────────────


def test_no_roles_section_without_role_library(tmp_mission):
    """未配置角色库（无 roles/ 目录）：planner/orchestrator/worker 都没有
    角色段——与既有行为逐字节一致。"""
    assets = AssetSnapshot.freeze(
        _PROMPTS, [tmp_mission / "skills", tmp_mission / "playbooks"]
    )
    state, _ = _state(tmp_mission)
    secs = render_sections("planner", state, {}, assets)
    assert "role_catalog" not in secs  # 目录段整段不产出
    w = {
        "goal": "g",
        "type": "web",
        "success_criteria": [],
        "untrusted_data": [],
        "clues": [],
        "lessons": [],
    }
    _, user = assemble("worker", state, w, assets)
    assert "# 角色" not in user


def test_no_role_section_when_task_unassigned(asset_dirs, tmp_mission):
    """配置了角色库但任务没指派 role：worker 段同样不出现角色/目录。"""
    skills, playbooks, roles = asset_dirs
    _md(roles / "recon.md", ROLE_RECON)
    assets = _freeze(skills, playbooks, roles)
    state, _ = _state(tmp_mission)
    w = dict(DRAFT_PAYLOAD, untrusted_data=[], clues=[], lessons=[])
    _, user = assemble("worker", state, w, assets)
    assert "# 角色" not in user
    _, p_user = assemble("planner", state, {}, assets)
    assert "角色目录" in p_user  # 目录照给：编排侧需要指派能力


# ── M1 主链路：指派 → 注入 → 落账 → 重放 ─────────────────────────


def test_draft_role_flows_through_create_task(asset_dirs, tmp_mission):
    skills, playbooks, roles = asset_dirs
    state, writer = _state(tmp_mission)
    d = TaskDraft.model_validate(dict(DRAFT_PAYLOAD, role="recon"))
    task = create_task(state, writer, d)
    assert task.role == "recon"
    # 落账：task.created payload 带 role；重放还原
    ev = [e for e in _events(tmp_mission) if e["type"] == "task.created"][-1]
    assert ev["payload"]["task"]["role"] == "recon"
    replayed = StoreView(tmp_mission).restore()
    assert replayed.tasks[task.id].role == "recon"


def test_worker_gets_role_manual_and_filtered_skills(asset_dirs, tmp_mission):
    skills, playbooks, roles = asset_dirs
    _md(roles / "recon.md", ROLE_RECON)
    _md(skills / "tagged.md", SKILL_TAGGED)
    assets = _freeze(skills, playbooks, roles)
    state, _ = _state(tmp_mission)
    w = dict(DRAFT_PAYLOAD, role="recon", untrusted_data=[], clues=[], lessons=[])
    secs = render_sections("worker", state, w, assets)
    assert "# 角色\n" in secs["role"] and "不盲扫" in secs["role"]
    assert "## role-only" in secs["skills"]  # 角色专属技能可见
    w_other = dict(w, role="web-app")
    secs2 = render_sections("worker", state, w_other, assets)
    assert "## role-only" not in secs2["skills"]  # 双过滤把专属技能挡住
    # 确定性：同 payload 两次组装逐字节相同
    assert render_sections("worker", state, w, assets) == secs


def test_unknown_role_visible_degradation(asset_dirs, tmp_mission):
    skills, playbooks, roles = asset_dirs
    _md(roles / "recon.md", ROLE_RECON)
    assets = _freeze(skills, playbooks, roles)
    state, _ = _state(tmp_mission)
    w = dict(DRAFT_PAYLOAD, role="typo-role", untrusted_data=[], clues=[], lessons=[])
    _, user = assemble("worker", state, w, assets)
    assert "typo-role" in user and "不在角色库" in user  # 可见降级


def test_brief_carries_role_and_applicable_skills(asset_dirs, tmp_mission):
    skills, playbooks, roles = asset_dirs
    _md(roles / "recon.md", ROLE_RECON)
    _md(skills / "tagged.md", SKILL_TAGGED)
    assets = _freeze(skills, playbooks, roles)
    state, writer = _state(tmp_mission)
    task = create_task(
        state, writer, TaskDraft.model_validate(dict(DRAFT_PAYLOAD, role="recon"))
    )
    brief = compile_briefing(state, task, assets)
    assert brief["role"] == "recon"
    assert "role-only" in brief["applicable_skills"]


# ── M4 playbook：数据驱动的技能文件，按任务类型注入 ───────────────


def test_playbook_injected_by_task_type(asset_dirs, tmp_mission):
    skills, playbooks, roles = asset_dirs
    _md(playbooks / "pb-recon.md", PLAYBOOK)
    assets = _freeze(skills, playbooks, roles)
    state, _ = _state(tmp_mission)
    recon = dict(DRAFT_PAYLOAD, untrusted_data=[], clues=[], lessons=[])
    web = dict(recon, type="web")
    assert (
        "第一阶段先锚点确认"
        in render_sections("worker", state, recon, assets)["skills"]
    )
    assert (
        "pb-recon" not in render_sections("worker", state, web, assets)["skills"]
    )  # 面外不注入
    _, p_user = assemble("planner", state, {}, assets)
    assert "pb-recon — t —" in p_user  # 目录可见给 planner


# ── 工具 ─────────────────────────────────────────────────────────


def _events(root):
    return [
        json.loads(x)
        for x in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
