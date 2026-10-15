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


# ---------------------------------------------------------------------------
# P0 并行化：并行执行、串行记账（hooks/消息顺序与串行版一致）
# ---------------------------------------------------------------------------


class _RecordingHooks:
    """记录 on_tool/on_result 调用顺序（验证串行记账按原始顺序成对回调）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def on_tool(self, name: str, args: dict) -> None:
        self.events.append(("tool", name))

    def on_result(self, call_id: str, out: str) -> None:
        self.events.append(("result", call_id))

    def on_finding(self, *args, **kwargs) -> None:
        pass


@pytest.mark.asyncio
async def test_engine_parallel_tool_calls(tmp_path: Path) -> None:
    """同轮多个独立工具调用：并行执行、串行记账。

    断言：tool 结果按原始 call_id 顺序 append；hooks.on_tool/on_result 也按
    原始顺序成对回调（warroom FIFO 配对与事件流顺序与串行版一致）。
    """

    a = tmp_path / "a.txt"
    a.write_text("AAA")
    b = tmp_path / "b.txt"
    b.write_text("BBB")

    class ParallelLLM(FakeLLM):
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, messages, tools, model) -> _Resp:
            self.calls += 1
            if self.calls == 1:
                return _Resp(
                    _Msg(
                        content="并行读两个文件 + 看当前目录。",
                        tool_calls=[
                            _TC("c1", "read_file", json.dumps({"path": str(a)})),
                            _TC("c2", "read_file", json.dumps({"path": str(b)})),
                            _TC("c3", "bash", json.dumps({"command": "pwd"})),
                        ],
                    )
                )
            return _Resp(_Msg(content="ANSWER: FLAG{parallel_ok}"))

    llm = ParallelLLM()
    hooks = _RecordingHooks()
    cfg = RunConfig(
        job_id="parallel",
        objective="测试任务全文",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=10,
        max_wall_s=60,
    )
    result = await Engine(cfg, llm=llm, hooks=hooks).run()

    assert result.status == "success"
    assert result.flag == "FLAG{parallel_ok}"
    assert llm.calls == 2

    msgs = result.messages
    # c) 工具结果按原始顺序 append 回同一条 messages
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_call_id"] == "c1"
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "c2"
    assert msgs[4]["role"] == "tool" and msgs[4]["tool_call_id"] == "c3"
    assert "AAA" in msgs[2]["content"]
    assert "BBB" in msgs[3]["content"]
    # bash pwd 输出跨平台规范化比较（Windows 有盘符大小写/短路径差异，不做字符串前缀断言）
    import os as _os

    assert _os.path.normcase(Path(msgs[4]["content"].strip()).resolve()) == _os.path.normcase(
        (tmp_path / "ws").resolve()
    )

    # 串行记账：hooks 按原始顺序成对交错（tool→result），事件流顺序与串行版一致
    assert hooks.events == [
        ("tool", "read_file"),
        ("result", "c1"),
        ("tool", "read_file"),
        ("result", "c2"),
        ("tool", "bash"),
        ("result", "c3"),
    ]


@pytest.mark.asyncio
async def test_engine_dependency_grouping_serializes(tmp_path: Path) -> None:
    """同轮 write_file + bash 引用其产物：依赖启发式必须拆组串行。

    若并行执行，bash(python3 hello.py) 可能先于 write_file 完成而报文件
    不存在；断言 bash 输出含 HELLO 证明"写→跑"被正确串行化。
    """

    class DepLLM(FakeLLM):
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, messages, tools, model) -> _Resp:
            self.calls += 1
            if self.calls == 1:
                return _Resp(
                    _Msg(
                        content="先写脚本再运行。",
                        tool_calls=[
                            _TC(
                                "w1",
                                "write_file",
                                json.dumps({"path": "hello.py", "content": "print('HELLO')"}),
                            ),
                            _TC("r1", "bash", json.dumps({"command": "python3 hello.py"})),
                        ],
                    )
                )
            return _Resp(_Msg(content="ANSWER: FLAG{dep_ok}"))

    llm = DepLLM()
    cfg = RunConfig(
        job_id="depgroup",
        objective="测试任务全文",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=10,
        max_wall_s=60,
    )
    result = await Engine(cfg, llm=llm).run()

    assert result.status == "success"
    msgs = result.messages
    assert msgs[2]["tool_call_id"] == "w1"
    assert msgs[2]["content"].startswith("OK:")
    assert msgs[3]["tool_call_id"] == "r1"
    assert "HELLO" in msgs[3]["content"]


def test_split_parallel_groups_dependency_heuristic() -> None:
    """分组启发式纯函数：独立调用同组；写→读/跑拆组；URL 不误判；同路径覆盖拆组。"""

    from secpt.runtime.engine import _split_parallel_groups

    def item(name: str, args: dict):
        return (None, name, args)  # tc 在分组里不参与计算

    # 独立 read_file ×2 + bash(pwd) → read_file 同组并行，bash 独立一组（方案 A：bash 串行）
    groups = _split_parallel_groups(
        [
            item("read_file", {"path": "a.txt"}),
            item("read_file", {"path": "b.txt"}),
            item("bash", {"command": "pwd"}),
        ]
    )
    assert len(groups) == 2
    assert len(groups[0]) == 2 and groups[0][0][1] == "read_file"
    assert len(groups[1]) == 1 and groups[1][0][1] == "bash"

    # write_file(hello.py) + bash(python3 hello.py) → 拆成两组（写→跑依赖）
    groups = _split_parallel_groups(
        [
            item("write_file", {"path": "hello.py", "content": ""}),
            item("bash", {"command": "python3 hello.py"}),
        ]
    )
    assert len(groups) == 2
    assert groups[0][0][1] == "write_file"
    assert groups[1][0][1] == "bash"

    # write_file(hello.py) + read_file(hello.py) → 拆成两组（写→读依赖）
    groups = _split_parallel_groups(
        [
            item("write_file", {"path": "hello.py", "content": ""}),
            item("read_file", {"path": "hello.py"}),
        ]
    )
    assert len(groups) == 2

    # URL token 不是本地路径引用 → 与 read_file 同组并行
    groups = _split_parallel_groups(
        [
            item("read_file", {"path": "a.txt"}),
            item("bash", {"command": "curl http://127.0.0.1:5000/"}),
        ]
    )
    assert len(groups) == 2  # read_file 组 + bash 独立组

    # 同路径两次 write_file → 覆盖依赖拆组
    groups = _split_parallel_groups(
        [
            item("write_file", {"path": "x.py", "content": "a"}),
            item("write_file", {"path": "x.py", "content": "b"}),
        ]
    )
    assert len(groups) == 2

    # 方案 A：两个 bash 互不依赖也各占一组（串行，不并行）——共享工作区防互踩
    groups = _split_parallel_groups(
        [
            item("bash", {"command": "curl http://10.0.0.1/a"}),
            item("bash", {"command": "python3 scan.py"}),
        ]
    )
    assert len(groups) == 2 and all(len(g) == 1 for g in groups)

    # 混合：write_file → bash → read_file → bash：bash 串行，write/read 各独立成组
    groups = _split_parallel_groups(
        [
            item("write_file", {"path": "a.txt", "content": "x"}),
            item("bash", {"command": "cat a.txt"}),
            item("read_file", {"path": "b.txt"}),
            item("bash", {"command": "ls"}),
        ]
    )
    assert len(groups) == 4
    assert [g[0][1] for g in groups] == ["write_file", "bash", "read_file", "bash"]


def test_ratelimit_group_gate() -> None:
    """组级限速门：组内 bash ≥2 时一次性过配额；单 bash 组不触发（不双重计数）。"""

    from secpt.runtime.engine import _ratelimit_group_gate

    def item(name: str, args: dict):
        return (None, name, args)

    class _RL:
        """假限速器：记录被检查的命令；blocked 返回非空。"""

        def __init__(self, blocked_hosts=()):
            self.seen = []
            self.blocked = set(blocked_hosts)

        def check(self, command):
            self.seen.append(command)
            return sorted(self.blocked) if self.blocked else []

    cfg = RunConfig(job_id="j", objective="", system_extra="", workspace=Path("/tmp"))

    # 单 bash 组（方案 A 常态）：不触发组级检查，无双重计数
    rl = _RL()
    cfg.ratelimit = rl
    assert _ratelimit_group_gate(cfg, [item("bash", {"command": "curl http://10.0.0.1/"})]) is None
    assert rl.seen == []

    # 多 bash 组：全部命令过 check；无拦截 → 放行
    rl2 = _RL()
    cfg.ratelimit = rl2
    group2 = [
        item("bash", {"command": "curl http://10.0.0.1/a"}),
        item("bash", {"command": "curl http://10.0.0.1/b"}),
    ]
    assert _ratelimit_group_gate(cfg, group2) is None
    assert rl2.seen == ["curl http://10.0.0.1/a", "curl http://10.0.0.1/b"]

    # 超限拦截：整组 bash 被拒（fail-closed）
    rl3 = _RL(blocked_hosts=["10.0.0.1"])
    cfg.ratelimit = rl3
    err = _ratelimit_group_gate(cfg, group2)
    assert err and "限速" in err and "10.0.0.1" in err

    # 限速器故障：fail-open 放行（观测不阻断）
    class _Boom:
        def check(self, command):
            raise RuntimeError("db locked")

    cfg.ratelimit = _Boom()
    assert _ratelimit_group_gate(cfg, group2) is None

    # 无限速器：放行
    cfg.ratelimit = None
    assert _ratelimit_group_gate(cfg, group2) is None


# ---------------------------------------------------------------------------
# ⑥ 完全重复调用自我纠偏：引擎按调用指纹喂入 StuckDetector 并注入 nudge
# ---------------------------------------------------------------------------


def test_call_key_order_independent() -> None:
    """调用指纹：同工具同参（含键序不同）同指纹；不同工具/参数不同指纹。"""
    from secpt.runtime.engine import _call_key

    assert _call_key("bash", {"command": "pwd"}) == _call_key("bash", {"command": "pwd"})
    assert _call_key("bash", {"a": 1, "b": 2}) == _call_key("bash", {"b": 2, "a": 1})
    assert _call_key("bash", {"command": "ls"}) != _call_key("read_file", {"path": "ls"})
    assert _call_key("bash", {"command": "ls"}) != _call_key("bash", {"command": "ls -l"})


@pytest.mark.asyncio
async def test_engine_nudges_on_repeated_identical_calls(tmp_path: Path) -> None:
    """同工具同参数、输出不变地连调 3 次 → 引擎注入"完全重复调用"提醒
    （比"无新信息"6 步阈值更早；1053 实测 holders×3 即此类空转）。"""

    class RepeatLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def call(self, messages, tools, model) -> _Resp:
            self.calls += 1
            if self.calls <= 3:
                return _Resp(
                    _Msg(
                        content="再看一眼当前目录。",
                        tool_calls=[_TC(f"c{self.calls}", "bash", json.dumps({"command": "pwd"}))],
                    )
                )
            return _Resp(_Msg(content="ANSWER: FLAG{dup_ok}"))

    llm = RepeatLLM()
    cfg = RunConfig(
        job_id="dup",
        objective="测试任务全文",
        system_extra="你是测试员。",
        workspace=tmp_path,
        max_steps=10,
        max_wall_s=60,
    )
    result = await Engine(cfg, llm=llm).run()

    assert result.status == "success"
    nudges = [
        m
        for m in result.messages
        if m.get("role") == "user" and "完全相同的工具和参数" in (m.get("content") or "")
    ]
    assert len(nudges) == 1  # 第 3 次相同调用后触发一次
