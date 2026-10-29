"""M5-10 知识库测试：出厂条目 / 关键词选择 / 截断渲染 / 注意力包接线。

知识库口径（设计文档 L43）：文件资产型、只读参考；选择必须是纯关键词命中
（同一输入同一输出，重放可复算），正文截断后进包且与"事实"章节分开渲染。
"""

from __future__ import annotations

import json
import sqlite3

from secpt.assets import (
    KnowledgeDoc,
    list_knowledge,
    load_knowledge,
    render_knowledge,
    select_knowledge,
)
from secpt.core.loop import PentestLoop
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.llms.client import Goal, LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import ledger
from secpt.store import migrate as migrate_store
from secpt.store.attention import build
from secpt.tools.registry import ToolRegistry


class TestKnowledgeLibrary:
    def test_bundled_docs_shipped(self) -> None:
        ids = list_knowledge()
        assert "port-scan" in ids and "http-probe" in ids

    def test_load_parses_front_matter(self) -> None:
        doc = load_knowledge("port-scan")
        assert doc is not None
        assert doc.title == "端口与服务识别"
        assert "端口" in doc.keywords and "nmap" in doc.keywords
        assert "banner" in doc.body

    def test_missing_doc_returns_none(self) -> None:
        assert load_knowledge("no-such-doc") is None

    def test_select_by_keyword_hits(self) -> None:
        assert [doc.id for doc in select_knowledge("对内网 10.0.0.5 做端口摸底")] == ["port-scan"]
        assert [doc.id for doc in select_knowledge("web 站点 http 指纹")] == ["http-probe"]

    def test_select_scores_and_caps(self) -> None:
        """命中数优先；limit 生效；同分按 id 稳定排序（可复算）。"""
        many = select_knowledge("nmap 端口 服务 扫描", limit=2)
        assert [doc.id for doc in many] == ["port-scan"]
        tie = select_knowledge("端口 http", limit=2)
        assert [doc.id for doc in tie] == ["http-probe", "port-scan"]  # 各中 1 条，按 id 稳定

    def test_no_hit_returns_empty(self) -> None:
        assert select_knowledge("写一份总结") == []
        assert select_knowledge("   ") == []

    def test_render_truncates_body(self) -> None:
        doc = KnowledgeDoc(id="d", title="长文档", body="x" * 100)
        lines = render_knowledge([doc], max_chars=10)
        assert lines == ["[长文档] " + "x" * 10 + "…"]

    def test_attention_pack_renders_knowledge_section_separately(self) -> None:
        pack = build(
            goal=Goal(target="t", objective="o", success_criteria="s"),
            step_no=1,
            plan_summary="（暂无）",
            recent=[],
            knowledge=["[端口与服务识别] 先粗扫再精扫"],
        )
        text = pack.render()
        assert "## 知识参考（只读方法论，不是本任务事实）" in text
        assert "[端口与服务识别] 先粗扫再精扫" in text


# ---------- 主循环接线 ----------------------------------------------------------


def _loop(fake: FakeLLM) -> tuple[PentestLoop, sqlite3.Connection]:
    conn = migrate_store.connect(":memory:")
    loop = PentestLoop(
        conn,
        MainLlm(LlmClient(fake)),
        ToolRegistry([]),
        exit_checker=ExitChecklist(),
    )
    return loop, conn


def test_loop_pack_carries_knowledge_when_goal_matches() -> None:
    goal = json.dumps(
        {"target": "10.0.0.5", "objective": "端口摸底", "success_criteria": "拿到 banner"},
        ensure_ascii=False,
    )
    done = json.dumps({"thought": "收工", "tool_calls": [], "done": True})
    fake = FakeLLM([goal, done])
    loop, conn = _loop(fake)
    loop.run("要对 10.0.0.5 做端口摸底")

    pack = fake.calls[1][1].content
    assert "知识参考（只读方法论，不是本任务事实）" in pack
    assert "端口与服务识别" in pack
    assert ledger.latest_task(conn).exit_kind == "normal"


def test_loop_pack_omits_knowledge_when_no_hit() -> None:
    """无关键词命中 = 不渲染该节（不给模型塞无关读物）。"""
    goal = json.dumps(
        {"target": "10.0.0.5", "objective": "整理材料", "success_criteria": "写一份说明"},
        ensure_ascii=False,
    )
    done = json.dumps({"thought": "收工", "tool_calls": [], "done": True})
    fake = FakeLLM([goal, done])
    loop, _conn = _loop(fake)
    loop.run("整理材料")

    assert "知识参考" not in fake.calls[1][1].content


# ---------- M5-16 可派角色清单 --------------------------------------------------


def test_role_catalog_lists_roles_for_dispatch() -> None:
    from secpt.assets import role_catalog

    lines = role_catalog()
    assert any(line.startswith("recon（侦察员）") for line in lines)
    assert any("port-recon" in line for line in lines)  # 技能名随清单可见
    assert all("你是侦察员" not in line for line in lines)  # instruction 正文不进清单


def test_loop_pack_carries_role_catalog() -> None:
    goal = json.dumps(
        {"target": "10.0.0.5", "objective": "整理材料", "success_criteria": "写一份说明"},
        ensure_ascii=False,
    )
    done = json.dumps({"thought": "收工", "tool_calls": [], "done": True})
    fake = FakeLLM([goal, done])
    loop, _conn = _loop(fake)
    loop.run("整理材料")

    pack = fake.calls[1][1].content
    assert "## 可派角色（agent_run 的 persona 取 id）" in pack
    assert "recon（侦察员）" in pack
