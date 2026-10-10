"""secpt chat REPL 测试。

覆盖：
- /tools 列出已注册工具（含 saml）
- ? 开头走问答分支（不创建 engagement）
- 自然语言任务走 EngagementRunner
- 会话上下文跨任务延续
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from secpt.cli.chat import ChatSession, _run_repl


def _make_input_sequence(lines: list[str]):
    """构造一个模拟 stdin 的异步输入函数。"""
    queue = list(lines)

    async def _fake_input(prompt: str) -> str:
        if not queue:
            raise EOFError  # 没有更多输入 → 退出
        return queue.pop(0)

    return _fake_input


def _make_session(tmp_path, backend: str = "memory") -> ChatSession:
    """构造一个已初始化的 ChatSession。"""
    session = ChatSession(
        session_id="test-session",
        backend=backend,
        budget=50.0,
        concurrency=3,
        fake=True,
    )
    return session


@pytest.fixture
async def chat_session(tmp_path):
    """初始化后的 session（memory 后端）。"""
    session = _make_session(tmp_path)
    await session.init()
    yield session
    await session.close()


# ---------- /tools ----------


@pytest.mark.asyncio
async def test_tools_command_lists_saml(chat_session, capsys):
    """/tools 应列出已注册工具，包含 saml。"""
    fake_input = _make_input_sequence(["/tools", "/exit"])

    with patch("secpt.cli.chat._make_input_fn", return_value=fake_input):
        await _run_repl(chat_session)

    captured = capsys.readouterr()
    assert "saml" in captured.out
    assert "crawler" in captured.out


# ---------- ? 问答 ----------


@pytest.mark.asyncio
async def test_question_branch_no_engagement(chat_session, capsys):
    """? 开头走问答分支，不应派发任务。"""
    fake_input = _make_input_sequence(["?扫到了什么", "/exit"])

    # patch run_task 检测是否被派发
    with (
        patch("secpt.cli.chat._make_input_fn", return_value=fake_input),
        patch("secpt.cli.chat.ChatSession.run_task", AsyncMock()) as mock_run,
    ):
        await _run_repl(chat_session)

    # run_task 不应被调用
    mock_run.assert_not_called()
    captured = capsys.readouterr()
    assert "扫到了什么" not in captured.out  # 输出不应包含任务派发标记


# ---------- 任务派发 ----------


@pytest.mark.asyncio
async def test_task_dispatched_to_engine(chat_session, capsys):
    """自然语言任务应调用 Engine 并打印摘要。"""
    fake_input = _make_input_sequence(["扫描目标", "/exit"])

    fake_result = {
        "stdout": "steps=3 wall=1.0s status=success",
        "stderr": "",
        "returncode": 0,
        "flag": "FLAG{test123}",
    }

    with (
        patch("secpt.cli.chat._make_input_fn", return_value=fake_input),
        patch(
            "secpt.cli.chat.ChatSession.run_task", AsyncMock(return_value=fake_result)
        ) as mock_run,
    ):
        await _run_repl(chat_session)

    mock_run.assert_called_once()
    captured = capsys.readouterr()
    assert "[任务]" in captured.out
    assert "FLAG{test123}" in captured.out


# ---------- 会话上下文延续 ----------


@pytest.mark.asyncio
async def test_session_context_persists(chat_session):
    """任务执行后会话上下文应非空，可用于后续问答。"""
    from secpt.runtime.engine import RunResult

    fake_result = RunResult(
        flag="FLAG{ctx_test}",
        steps=1,
        wall_s=0.1,
        status="success",
        messages=[],
    )

    with patch("secpt.runtime.engine.Engine.run", AsyncMock(return_value=fake_result)):
        await chat_session.run_task("第一次扫描")
    assert len(chat_session.context) > 0

    # 第二次任务（应能引用第一次的发现）
    with patch("secpt.runtime.engine.Engine.run", AsyncMock(return_value=fake_result)):
        await chat_session.run_task("第二次深挖")
    assert len(chat_session.context) >= 2

    # 上下文应包含第一次的关键信息
    ctx_str = "\n".join(chat_session.context)
    assert "FLAG{ctx_test}" in ctx_str or "任务完成" in ctx_str
