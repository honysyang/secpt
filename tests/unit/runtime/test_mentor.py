"""PA2 导师止损：卡死 nudge 时先调强模型诊断换路线，失败/未启用回退固定文案。

- tools=None 区分导师调用与主链调用（同一 llm 双用）
- 配额/假 LLM/未配模型 → 不发起导师调用
- 导师成功 → 注入【导师建议】；失败 → 注入固定文案，主链不受影响
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secpt.runtime.engine import Engine, RunConfig


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, tc_id: str, name: str, arguments: str) -> None:
        self.id = tc_id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content: str, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None


class _Resp:
    def __init__(self, msg: _Msg) -> None:
        self.choices = [type("c", (), {"message": msg})()]


class MentorAwareLLM:
    """主链按脚本返回；tools=None 的导师调用单独计数/可注入失败。"""

    def __init__(self, main: list[_Msg], advice: str = "换路线：试 8080 端口", fail_mentor=False):
        self._main = list(main)
        self.advice = advice
        self.fail_mentor = fail_mentor
        self.mentor_calls = 0

    async def call(self, messages: list, tools: list, model: str) -> _Resp:
        if tools is None:  # 导师调用
            self.mentor_calls += 1
            if self.fail_mentor:
                raise RuntimeError("mentor down")
            return _Resp(_Msg(self.advice))
        return _Resp(self._main.pop(0))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "SECPT_DELEGATE",
        "SECPT_TOOL_POOL",
        "SECPT_MENTOR",
        "SECPT_MENTOR_MODEL",
        "SECPT_ESCALATE_MODEL",
        "SECPT_FAKE_LLM",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SECPT_AUTO_STRUCTURE", "0")


def _mk_engine(tmp_path: Path, llm) -> Engine:
    cfg = RunConfig(
        job_id="mentor",
        objective="解出目标 flag",
        system_extra="",
        workspace=tmp_path,
        max_steps=10,
        stuck_threshold=1,  # 尽早触发卡死切换器（重复调用 → nudge）
    )

    def read_file(args: dict) -> str:
        return "same data"  # 完全重复调用 → dup 触发器

    return Engine(cfg, llm=llm, tools={"read_file": read_file})


def _main_script(rounds: int = 3) -> list[_Msg]:
    """rounds 轮完全相同的调用（触发 dup 卡死判据），随后 ANSWER。"""
    out = [
        _Msg("", [_TC(f"t{i}", "read_file", '{"path": "same.txt"}')]) for i in range(1, rounds + 1)
    ]
    out.append(_Msg("ANSWER: done", []))
    return out


def _run(eng: Engine):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(eng.run())


def test_mentor_advice_injected_on_stuck(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_MENTOR", "1")
    monkeypatch.setenv("SECPT_MENTOR_MODEL", "mentor-x")
    llm = MentorAwareLLM(_main_script())
    eng = _mk_engine(tmp_path, llm)
    result = _run(eng)
    assert result.status == "success"
    assert llm.mentor_calls == 1
    advices = [
        m
        for m in result.messages
        if isinstance(m, dict) and m.get("role") == "user" and "【导师建议】" in m["content"]
    ]
    assert len(advices) == 1 and "8080" in advices[0]["content"]


def test_mentor_failure_falls_back_to_canned(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_MENTOR", "1")
    monkeypatch.setenv("SECPT_MENTOR_MODEL", "mentor-x")
    llm = MentorAwareLLM(_main_script(), fail_mentor=True)
    eng = _mk_engine(tmp_path, llm)
    result = _run(eng)
    assert result.status == "success"  # 导师挂了不伤主链
    assert llm.mentor_calls == 1
    advices = [
        m
        for m in result.messages
        if isinstance(m, dict) and m.get("role") == "user" and "【导师建议】" in m["content"]
    ]
    assert advices == []


def test_mentor_disabled_by_default(tmp_path):
    llm = MentorAwareLLM(_main_script())
    eng = _mk_engine(tmp_path, llm)
    result = _run(eng)
    assert result.status == "success"
    assert llm.mentor_calls == 0  # 未开启 → 不发起导师调用


def test_fake_llm_skips_mentor(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_MENTOR", "1")
    monkeypatch.setenv("SECPT_MENTOR_MODEL", "mentor-x")
    monkeypatch.setenv("SECPT_FAKE_LLM", "1")
    llm = MentorAwareLLM(_main_script())
    eng = _mk_engine(tmp_path, llm)
    result = _run(eng)
    assert result.status == "success"
    assert llm.mentor_calls == 0  # 假 LLM 干跑不烧导师调用
