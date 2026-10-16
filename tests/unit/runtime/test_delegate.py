"""delegate 子智能体工具：一次委托 / 独立上下文 / 禁递归 / 工作区挂靠 / 环境变量恢复。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import ClassVar


def _resp(content="", tool_calls=None):
    class _Fn:
        def __init__(self, name, args):
            self.name, self.arguments = name, json.dumps(args)

    class _TC:
        def __init__(self, name, args):
            self.id = f"tc-{name}"
            self.function = _Fn(name, args)

    class _Msg:
        def __init__(self):
            self.content = content
            self.tool_calls = [_TC(n, a) for n, a in (tool_calls or [])]
            self.reasoning_content = None

    class _R:
        choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

    return _R()


class _DelegatingLLM:
    """父：第 1 步 delegate，第 2 步 ANSWER；子（system 含"子智能体"）：bash 后 ANSWER。

    tools 参数逐次记录——用于断言子链工具集不含 delegate（结构性禁递归）。
    """

    def __init__(self, child_answers: bool = True):
        self.parent_step = 0
        self.child_step = 0
        self.child_tools: list[list[str]] = []
        self._child_answers = child_answers

    async def call(self, messages, tools, model):
        names = [(t.get("function") or {}).get("name") for t in tools]
        if "子智能体" in str(messages[0].get("content") or ""):
            self.child_tools.append(names)
            self.child_step += 1
            if self._child_answers and self.child_step >= 2:
                return _resp("ANSWER: CHILD_OK")
            return _resp("", [("bash", {"command": "echo child-work"})])
        self.parent_step += 1
        if self.parent_step == 1:
            return _resp("", [("delegate", {"task": "列出工作区文件并报告"})])
        return _resp("ANSWER: PARENT_OK")


def _mk_cfg(tmp_path, job="dlp"):
    from secpt.runtime.engine import RunConfig

    return RunConfig(
        job_id=job,
        objective="父任务",
        system_extra="父系统提示",
        workspace=tmp_path,
        max_steps=6,
        max_wall_s=60,
    )


class TestDelegate:
    def test_off_by_default(self, tmp_path, monkeypatch):
        from secpt.runtime.engine import Engine

        monkeypatch.delenv("SECPT_DELEGATE", raising=False)
        eng = Engine(_mk_cfg(tmp_path), llm=object())
        assert "delegate" not in eng.tools

    def test_child_cannot_recurse(self, tmp_path, monkeypatch):
        from secpt.runtime.engine import Engine

        monkeypatch.setenv("SECPT_DELEGATE", "1")
        child = Engine(_mk_cfg(tmp_path, "dlp-dl1"), llm=object(), enable_delegate=False)
        assert "delegate" not in child.tools  # 结构性禁递归（不靠提示词纪律）

    def test_roundtrip_isolation_and_env_restore(self, tmp_path, monkeypatch):
        from secpt.runtime.engine import Engine

        monkeypatch.setenv("SECPT_DELEGATE", "1")
        monkeypatch.setenv("SECPT_DELEGATE_STEPS", "6")
        llm = _DelegatingLLM()
        result = asyncio.run(Engine(_mk_cfg(tmp_path), llm=llm).run())

        # 父链正常收尾，delegate 结论以工具结果返回（含子链 ANSWER）
        assert result.flag == "PARENT_OK"
        tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
        assert any("CHILD_OK" in str(m.get("content")) for m in tool_msgs)
        assert any("[delegate-1 结论]" in str(m.get("content")) for m in tool_msgs)

        # 子链工作区挂靠父 ws 下（父可 read_file 取产物），transcript 独立落盘
        child_ws = tmp_path / "ws" / "delegate-1" / "ws"
        assert (child_ws / "transcript.json").is_file()
        child_msgs = json.loads((child_ws / "transcript.json").read_text(encoding="utf-8"))
        assert any("CHILD_OK" in str(m.get("content")) for m in child_msgs)

        # 禁递归穿透：子链 (所有调用) 的工具集不含 delegate
        assert llm.child_tools and all("delegate" not in names for names in llm.child_tools)

        # 环境变量恢复：子链覆写进程级 SECPT_JOB_ID 后父链环境被还原
        assert os.environ.get("SECPT_JOB_ID") == "dlp"

    def test_child_exhausted_reported(self, tmp_path, monkeypatch):
        """子链拿不到 ANSWER（步数耗尽）→ 结论标注 status，父链不受影响。"""
        from secpt.runtime.engine import Engine

        monkeypatch.setenv("SECPT_DELEGATE", "1")
        monkeypatch.setenv("SECPT_DELEGATE_STEPS", "5")
        llm = _DelegatingLLM(child_answers=False)  # 子链永不 ANSWER → step_exceeded
        result = asyncio.run(Engine(_mk_cfg(tmp_path), llm=llm).run())
        assert result.flag == "PARENT_OK"
        tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
        assert any("status=step_exceeded" in str(m.get("content")) for m in tool_msgs)

    def test_empty_task_rejected(self, tmp_path, monkeypatch):
        from secpt.runtime.engine import make_delegate_tool

        monkeypatch.setenv("SECPT_DELEGATE", "1")
        _schema, fn = make_delegate_tool(_mk_cfg(tmp_path), llm=object())
        out = asyncio.run(fn({"task": "  "}))
        assert out.startswith("Error:") and "自包含" in out
