"""M5 文件资产库契约测试：prompts 装载链 / skills 引用链 / worker 挂载与降级。

覆盖四件事（对应设计文档 L41-45 六库裁剪与 L11 worker 分配面）：
1. 出厂资产随包分发且可枚举（prompts/skills 各有出厂件）；
2. 查找链：``SECPT_ASSETS_DIR`` 覆盖目录胜过包内资产；缺件 E_ASSET 拒跑不返空串；
3. 引用链 skills ← prompts：技能引用的纪律片段能展开，断链报 E_ASSET；
4. worker 领单挂载：技能正文 + 纪律片段进系统话术；缺件 audit 留痕后继续领单。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from secpt.assets import (
    list_prompts,
    list_roles,
    list_skills,
    load_role,
    load_skill,
    prompt_text,
    render_prompt,
    role_fragments,
    skill_fragments,
)
from secpt.core.mission_board import Mission, MissionBoard, MissionResult
from secpt.errors import SecptError
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.worker import Worker
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import AgentRunTool
from secpt.tools.registry import ToolRegistry

_BUNDLED_PROMPTS = (
    "main.system",
    "main.entrance",
    "worker.system",
    "coach.propose",
    "coach.review",
    "distiller",
    "reporter",
    "skills.evidence",
)


# ---------- prompts 资产库 ------------------------------------------------------


class TestPromptLibrary:
    def test_bundled_prompts_are_shipped(self) -> None:
        """出厂资产随包分发：六个器官话术 + 一条共用纪律片段全部在包内可枚举。"""
        ids = list_prompts()
        for expected in _BUNDLED_PROMPTS:
            assert expected in ids, f"出厂缺失 prompt 资产：{expected}"
        assert "Orchestrator" in prompt_text("main.system")

    def test_render_substitutes_known_and_keeps_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已知占位替换；未知占位原样保留（写错字段名要看得见，不能被抹空）。"""
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "demo.md").write_text("hi {who} {unknown}", encoding="utf-8")
        monkeypatch.setenv("SECPT_ASSETS_DIR", str(tmp_path))
        assert render_prompt("demo", who="A") == "hi A {unknown}"

    def test_override_dir_wins_over_bundled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """运维覆盖目录优先：同 id 文件以覆盖目录为准（出厂件不被改也不被读）。"""
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "main.system.md").write_text("覆盖后的编排话术", encoding="utf-8")
        monkeypatch.setenv("SECPT_ASSETS_DIR", str(tmp_path))
        assert prompt_text("main.system") == "覆盖后的编排话术"

    def test_missing_prompt_is_e_asset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺件拒跑：空话术会让 LLM 行为不可解释，绝不静默返回空串。"""
        monkeypatch.setenv("SECPT_ASSETS_DIR", str(tmp_path))
        with pytest.raises(SecptError) as info:
            prompt_text("no.such.prompt")
        assert info.value.code == "E_ASSET"


# ---------- skills 资产库与引用链 -----------------------------------------------


class TestSkillLibrary:
    def test_bundled_skills_shipped(self) -> None:
        assert "port-recon" in list_skills()
        assert "web-surface" in list_skills()

    def test_reference_chain_expands_prompt_fragments(self) -> None:
        """skills ← prompts：技能引用的纪律片段能按 id 展开成原文。"""
        skill = load_skill("port-recon")
        assert skill is not None
        assert skill.name == "端口摸底"
        assert skill.prompts == ("skills.evidence",)
        assert "端口" in skill.body
        fragments = skill_fragments(skill)
        assert len(fragments) == 1 and "证据纪律" in fragments[0]

    def test_missing_skill_returns_none(self) -> None:
        """缺件返回 None（挂载侧降级留痕），与 prompt 的 E_ASSET 口径不同。"""
        assert load_skill("no-such-skill") is None

    def test_broken_reference_is_e_asset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """断链（技能声称引用某纪律但资产不存在）必须报错，不能当"没有纪律"。"""
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "broken.md").write_text(
            "---\nid: broken\nname: 断链技能\nprompts:\n  - ghost.rule\n---\n正文\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SECPT_ASSETS_DIR", str(tmp_path))
        skill = load_skill("broken")
        assert skill is not None
        with pytest.raises(SecptError) as info:
            skill_fragments(skill)
        assert info.value.code == "E_ASSET"


# ---------- 角色库（who+how 打包） ----------------------------------------------


class TestRoleLibrary:
    def test_bundled_role_shipped_with_reference_chain(self) -> None:
        """角色包引用 skills 与 prompts：三库链路在同一条资产上闭合。"""
        assert "recon" in list_roles()
        role = load_role("recon")
        assert role is not None
        assert role.name == "侦察员"
        assert role.skills == ("port-recon", "web-surface")
        assert role.prompts == ("skills.evidence",)
        assert "侦察员" in role.instruction
        assert "证据纪律" in role_fragments(role)[0]

    def test_missing_role_returns_none(self) -> None:
        assert load_role("no-such-role") is None

    def test_md_role_uses_body_as_instruction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """md 角色包：正文即 instruction（允许"先写 who/how、后补元数据"）。"""
        roles = tmp_path / "roles"
        roles.mkdir()
        (roles / "scout.md").write_text(
            "---\nid: scout\nname: 斥候\nskills:\n  - port-recon\n---\n只管看，不动手。\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SECPT_ASSETS_DIR", str(tmp_path))
        role = load_role("scout")
        assert role is not None
        assert role.instruction == "只管看，不动手。"
        assert role.skills == ("port-recon",)


# ---------- worker 挂载技能 / 角色 ---------------------------------------------


def _step(thought: str, *, done: bool = False) -> str:
    return json.dumps(
        {"thought": thought, "plan_updates": [], "tool_calls": [], "done": done},
        ensure_ascii=False,
    )


def _worker_env(script: list[object]) -> tuple[Worker, Mission, FakeLLM, sqlite3.Connection]:
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, input_text="技能挂载演示", goal="摸扫")
    fake = FakeLLM(list(script))
    worker = Worker(llm=LlmClient(fake), dispatcher=ToolRegistry([]), conn=conn)
    mission = Mission(persona="worker", goal="摸扫一圈", step_budget=1, task_id=task.id)
    return worker, mission, fake, conn


def test_worker_injects_skill_body_and_discipline() -> None:
    """挂载技能：系统话术携带技能正文与引用的纪律片段（skills ← prompts 生效）。"""
    worker, mission, fake, conn = _worker_env([_step("收工", done=True)])
    mounted = mission.model_copy(update={"skills": ("port-recon",)})
    result = worker.run(mounted)

    assert result.ok and result.finished
    system = fake.calls[0][0].content
    assert "挂载技能：端口摸底" in system
    assert "证据纪律" in system  # 引用的 prompt 片段确实进来了
    entry = conn.execute(
        "SELECT what FROM audit_log WHERE actor = 'workerA' ORDER BY rowid LIMIT 1"
    ).fetchone()["what"]
    assert "skills=port-recon" in entry  # 领单行可复算挂了哪些技能


def test_worker_missing_skill_degrades_with_audit() -> None:
    """缺件不阻断领单，但必须留痕：audit 记"技能库缺件"。"""
    worker, mission, fake, conn = _worker_env([_step("收工", done=True)])
    mounted = mission.model_copy(update={"skills": ("ghost-skill",)})
    result = worker.run(mounted)

    assert result.ok and result.finished
    assert "挂载技能" not in fake.calls[0][0].content
    whats = [
        row["what"]
        for row in conn.execute(
            "SELECT what FROM audit_log WHERE actor = 'workerA' ORDER BY rowid"
        ).fetchall()
    ]
    assert any("技能库缺件：ghost-skill" in what for what in whats)


def test_mission_skills_default_empty_keeps_old_semantics() -> None:
    assert Mission(goal="g").skills == ()


def test_worker_mounts_role_by_persona() -> None:
    """persona 命中角色库 → 整包挂上：行事风格 + 角色引用的技能 + 纪律片段。"""
    worker, mission, fake, conn = _worker_env([_step("收工", done=True)])
    mounted = mission.model_copy(update={"persona": "recon"})
    result = worker.run(mounted)

    assert result.ok and result.finished
    system = fake.calls[0][0].content
    assert "## 角色：侦察员" in system
    assert "优先只读侦察" in system  # instruction 在场
    assert "挂载技能：端口摸底" in system  # 角色 skills 自动挂
    assert "挂载技能：web 面初筛" in system
    entry = conn.execute(
        "SELECT what FROM audit_log WHERE actor = 'workerA' ORDER BY rowid LIMIT 1"
    ).fetchone()["what"]
    assert "role=侦察员" in entry


def test_worker_unknown_persona_keeps_free_text() -> None:
    """persona 不命中角色库 = 自由文本（M3-5 老语义守恒，不阻断派单）。"""
    worker, mission, fake, _conn = _worker_env([_step("收工", done=True)])
    mounted = mission.model_copy(update={"persona": "自由文本角色"})
    result = worker.run(mounted)

    assert result.ok
    assert "## 角色：" not in fake.calls[0][0].content


def test_worker_unions_role_skills_and_mission_skills_without_dup() -> None:
    worker, mission, fake, _conn = _worker_env([_step("收工", done=True)])
    mounted = mission.model_copy(update={"persona": "recon", "skills": ("port-recon",)})
    worker.run(mounted)

    system = fake.calls[0][0].content
    assert system.count("挂载技能：端口摸底") == 1  # 角色 ∪ 工单 保序去重


# ---------- agent_run 把 skills 带进工单 ---------------------------------------


class _CaptureWorker:
    """最小 worker 替身：只记工单、立即如实收工（板级契约由 test_mission_board 覆盖）。"""

    def __init__(self, seen: list[Mission]) -> None:
        self._seen = seen

    def run(self, mission: Mission) -> MissionResult:
        self._seen.append(mission)
        return MissionResult(
            mission_id=mission.mission_id,
            ok=True,
            finished=True,
            steps_taken=1,
            findings_count=0,
            summary="capture",
        )


@pytest.fixture()
def capture_board() -> Iterator[tuple[MissionBoard, list[Mission], sqlite3.Connection]]:
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "派单演示", goal="g")
    seen: list[Mission] = []
    board = MissionBoard(
        conn,
        lambda _index: _CaptureWorker(seen),
        size=1,
        task_resolver=lambda _c: task.id,
    )
    try:
        yield board, seen, conn
    finally:
        board.shutdown()
        conn.close()


def test_agent_run_parses_comma_separated_skills(
    capture_board: tuple[MissionBoard, list[Mission], sqlite3.Connection],
) -> None:
    """agent_run 的 skills 参数是逗号清单：空白项丢弃、顺序保留。"""
    board, seen, _conn = capture_board
    tool = AgentRunTool(board=board)
    args: dict[str, Any] = {
        "mission": "扫一把",
        "persona": "recon",
        "step_budget": "2",
        "skills": "port-recon, web-surface ,",
        "coach_after_failures": "2",
    }
    out = tool.run(args)
    assert out.exit_code == 0
    assert seen[0].skills == ("port-recon", "web-surface")
    assert seen[0].coach_after_failures == 2  # M5-13：教练接入门槛随工单进板
