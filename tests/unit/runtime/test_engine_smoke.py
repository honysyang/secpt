"""Engine 主循环冒烟测试：假 LLM 验证四步语义。

a) messages 整条 append，从不 rebuild
b) assistant 消息原样 append（含 content 与 tool_calls）
c) 每个 tool_call 的结果 append 回同一条 messages
d) 模型以 ANSWER: <结论> 自宣告终止（flag 样式不固定，不做正则判定）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from secpt.runtime.engine import Engine, RunConfig


class _Fn:
    def __init__(self, name: str, arguments: str, tc_id: str) -> None:
        self.name = name
        self.arguments = arguments
        self._id = tc_id


class _TC:
    def __init__(self, tc_id: str, name: str, arguments: str) -> None:
        self.id = tc_id
        self.function = _Fn(name, arguments, tc_id)


class _Msg:
    def __init__(
        self, content: str, tool_calls: list | None = None, reasoning_content: str | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = reasoning_content


class _Resp:
    def __init__(self, msg: _Msg) -> None:
        self.choices = [type("c", (), {"message": msg})()]


class FakeLLM:
    """第一步返回固定 tool_call（write_file 写一个含 flag 的文件），第二步返回 ANSWER。"""

    def __init__(self, work_dir: Path) -> None:
        self.calls = 0
        self.work_dir = work_dir

    async def call(self, messages: list, tools: list, model: str) -> _Resp:
        self.calls += 1
        # 请求中不得出现 reasoning_content
        for m in messages:
            assert "reasoning_content" not in m
        if self.calls == 1:
            target = self.work_dir / "flag.txt"
            args = json.dumps({"path": str(target), "content": "FLAG{smoke_test_ok}"})
            return _Resp(
                _Msg(
                    content="我来准备脚本。",
                    tool_calls=[_TC("call_1", "write_file", args)],
                    reasoning_content="审计用推理内容",
                )
            )
        return _Resp(_Msg(content="ANSWER: FLAG{smoke_test_ok}"))


@pytest.mark.asyncio
async def test_engine_smoke(tmp_path: Path) -> None:
    llm = FakeLLM(tmp_path / "ws")
    cfg = RunConfig(
        job_id="smoke",
        objective="测试任务全文",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=10,
        max_wall_s=60,
    )
    engine = Engine(cfg, llm=llm)
    result = await engine.run()

    # d) flag 命中立即退出
    assert result.status == "success"
    assert result.flag == "FLAG{smoke_test_ok}"
    assert result.steps == 2
    assert llm.calls == 2

    msgs = result.messages
    # a)+b) 第一条是 system，assistant 整条 append（含 reasoning_content 落盘）
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "我来准备脚本。"
    assert msgs[1]["reasoning_content"] == "审计用推理内容"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "write_file"
    # c) tool 结果 append 回同一条 messages（write_file 输出含写入字节数，不含 flag，
    #    flag 由第二步 ANSWER 命中）
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["tool_call_id"] == "call_1"
    assert msgs[2]["content"].startswith("OK:")
    assert msgs[3]["role"] == "assistant"

    # transcript 已落盘
    transcript = tmp_path / "ws" / "transcript.json"
    assert transcript.is_file()
    on_disk = json.loads(transcript.read_text())
    assert on_disk == msgs
    assert result.flag == "FLAG{smoke_test_ok}"


@pytest.mark.asyncio
async def test_tool_output_flag_is_candidate_not_exit(tmp_path: Path) -> None:
    """工具输出里出现 flag 形态（含源码占位符）只记候选，不退出；
    直到模型自己在文本中宣告真 flag 才结束。"""

    src = tmp_path / "src" / "app.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        'FLAG="{test_flag}"  # 默认占位\n真实 FLAG{e946a54f-75cf-6d73-9163-ba0959c2dafc}\n',
        encoding="utf-8",
    )

    class ToolFlagLLM(FakeLLM):
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, messages: list, tools: list, model: str) -> _Resp:
            self.calls += 1
            for m in messages:
                assert "reasoning_content" not in m
            if self.calls == 1:
                args = json.dumps({"path": str(src)})
                return _Resp(
                    _Msg(
                        content="读一下源码。",
                        tool_calls=[_TC("call_1", "read_file", args)],
                    )
                )
            return _Resp(_Msg(content="ANSWER: FLAG{e946a54f-75cf-6d73-9163-ba0959c2dafc}"))

    llm = ToolFlagLLM()
    cfg = RunConfig(
        job_id="toolflag",
        objective="测试任务全文",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=10,
        max_wall_s=60,
    )
    result = await Engine(cfg, llm=llm).run()

    # 第 1 步工具输出含占位 flag 不退出；第 2 步模型宣告才结束
    assert result.status == "success"
    assert result.flag == "FLAG{e946a54f-75cf-6d73-9163-ba0959c2dafc}"
    assert result.steps == 2
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_placeholder_flag_in_assistant_ignored(tmp_path: Path) -> None:
    """assistant 文本里的占位符 flag（FLAG{...} / test_flag）不视为命中。"""

    class PlaceholderLLM(FakeLLM):
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, messages: list, tools: list, model: str) -> _Resp:
            self.calls += 1
            if self.calls == 1:
                return _Resp(_Msg(content="先找 FLAG{...} 这类模板占位，忽略它"))
            return _Resp(_Msg(content="ANSWER: FLAG{real_uid_ok}"))

    llm = PlaceholderLLM()
    cfg = RunConfig(
        job_id="phflag",
        objective="测试任务全文",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=10,
        max_wall_s=60,
    )
    result = await Engine(cfg, llm=llm).run()

    assert result.status == "success"
    assert result.flag == "FLAG{real_uid_ok}"
    assert result.steps == 2
    assert llm.calls == 2
