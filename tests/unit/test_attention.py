"""attention.build / render 契约：纯函数投影，M1 边界字段恒缺省。"""

from __future__ import annotations

from secpt.llms.client import Goal
from secpt.store import attention
from secpt.store.ledger import TicketRow

GOAL = Goal(
    target="192.168.1.10",
    objective="确认 web 服务暴露面",
    success_criteria="列出存活端口与 banner",
)


def _ticket(seq: int, tool_name: str = "nmap", exit_code: int | None = 0) -> TicketRow:
    return TicketRow(
        id=f"t{seq}",
        task_id="task",
        run_id="run",
        step_id="step",
        tool_name=tool_name,
        args_json="{}",
        exit_code=exit_code,
        summary=f"第{seq}条小票",
        raw_ref="r",
        created_at=seq,
    )


def test_build_projects_only_pack_fields() -> None:
    pack = attention.build(GOAL, 3, "暂无规划", [_ticket(1), _ticket(2, exit_code=None)])
    assert len(pack.recent_tickets) == 2
    assert pack.recent_tickets[1].exit_code is None
    # M1 边界：双图摘要恒空/经验不进包不进包
    assert pack.graph_summary == ""
    assert pack.experience_snapshot == []
    # goal 原样保留三问结构
    assert pack.goal.target == GOAL.target
    assert pack.goal.success_criteria == GOAL.success_criteria


def test_render_contains_three_questions_and_tickets() -> None:
    pack = attention.build(GOAL, 2, "A 系列：先扫端口", [_ticket(7)])
    text = pack.render()
    assert "目标: 192.168.1.10" in text
    assert "目的: 确认 web 服务暴露面" in text
    assert "成功标准: 列出存活端口与 banner" in text
    assert "当前第 2 步" in text
    assert "A 系列：先扫端口" in text
    assert "- [nmap] exit=0: 第7条小票" in text


def test_render_no_tickets_shows_placeholder_line() -> None:
    text = attention.build(GOAL, 1, "暂无", []).render()
    assert "（本任务还没有任何小票）" in text


def test_render_graph_summary_only_when_present() -> None:
    with_graph = attention.build(GOAL, 1, "暂无", [], graph_summary="nodes: 双图占位").render()
    assert "## 双图摘要" in with_graph
    no_graph = attention.build(GOAL, 1, "暂无", []).render()
    assert "## 双图摘要" not in no_graph


def test_render_hidden_sections_when_fields_absent() -> None:
    text = attention.build(GOAL, 1, "", []).render()
    assert "## 可用经验" not in text
    assert "（暂无）" in text  # plan_summary 空时给模型明示占位
