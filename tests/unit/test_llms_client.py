"""W1-2 contract test：统一 chat 协议（开发文档 §6.1 + 错误矩阵 §9）。

覆盖面：
- 正常路径：合法 JSON → 强类型 StepOutput；provider 文本带 ```json 围栏也能吃。
- schema 带错重试：坏 JSON / 缺字段 → 重试≤2 修复成功；重试消息是 append（原列表不动）。
- schema 重试耗尽：3 次全坏 → LlmError(E_SCHEMA)。
- 超时指数退避：TimeoutError ×3 → LlmError(E_TIMEOUT_LLM)；退避间隔 1/2/4 倍基线。
- tool_calls ≤4 约束在 schema 边界生效。
- FakeLLM 脚本耗尽显式报错，调用记录可断言。
"""

from __future__ import annotations

import json
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from secpt.errors import LlmError
from secpt.llms.client import LlmClient, Message, StepOutput
from secpt.llms.fake import FakeLLM


def _step_payload(**overrides: object) -> str:
    """一段合法 StepOutput JSON；overrides 用于在测试里当下改单字段值。"""
    data: dict[str, object] = {
        "thought": "先扫端口",
        "plan_updates": [],
        "tool_calls": [],
        "done": False,
    }
    data.update(overrides)
    return json.dumps(data)


def _client(llm: FakeLLM) -> LlmClient:
    """公共 fixture：退避基线压到 1ms，让超时测试不真睡。"""
    return LlmClient(llm, backoff_base_s=0.001)


_MESSAGES = [
    Message(role="system", content="你是 Orchestrator"),
    Message(role="user", content="go"),
]


class TestHappyPath:
    def test_valid_json_returns_typed_step_output(self) -> None:
        llm = FakeLLM([_step_payload(tool_calls=[{"name": "nmap", "args": {"ports": "80"}}])])
        out = _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert out.thought == "先扫端口"
        assert out.tool_calls[0].name == "nmap"
        assert out.done is False
        assert llm.call_count == 1

    def test_markdown_fence_is_stripped(self) -> None:
        llm = FakeLLM([f"```json\n{_step_payload()}\n```"])
        out = _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert out.thought == "先扫端口"

    def test_tool_calls_over_four_rejected_by_schema(self) -> None:
        data = _step_payload(tool_calls=[{"name": "t"}] * 5)
        with pytest.raises(ValidationError):
            StepOutput.model_validate(json.loads(data))


class TestSchemaRetry:
    def test_bad_json_then_fixed_passes_with_retry(self) -> None:
        llm = FakeLLM(["这不是 JSON", _step_payload()])
        out = _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert out.thought == "先扫端口"
        assert llm.call_count == 2  # 1 次初调 + 1 次带错重试
        assert llm.remaining == 0

    def test_retry_messages_carry_error_and_are_append_only(self) -> None:
        llm = FakeLLM(["垃圾", _step_payload()])
        before = list(_MESSAGES)
        _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert _MESSAGES == before, "调用方的 messages 不得被原地修改"
        second_call = llm.calls[1]
        assert second_call[:2] == before  # 原消息保持在前
        assert second_call[2].role == "assistant"  # 跟上坏输出原文
        assert second_call[3].role == "user"
        assert "校验" in second_call[3].content

    def test_missing_field_retried_then_exhausted_raises_e_schema(self) -> None:
        # thought 实为 int：3 次全是 schema 违规，重试耗尽必须 E_SCHEMA 收场。
        bad = json.dumps({"thought": 12345})
        llm = FakeLLM(["{}", "null", bad])
        with pytest.raises(LlmError) as info:
            _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert info.value.code == "E_SCHEMA"
        assert info.value.context["attempts"] == 3
        assert "valid string" in info.value.context["last_error"]
        assert llm.call_count == 3


class TestTimeoutBackoff:
    def test_timeout_exhausts_to_e_timeout_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 纯断言退避次数：不真睡（backoff 已压到 1ms 量级，直接跑也接近 0）。
        llm = FakeLLM([TimeoutError("provider 超时")] * 3)
        with pytest.raises(LlmError) as info:
            _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert info.value.code == "E_TIMEOUT_LLM"
        assert info.value.context["attempts"] == 3
        assert llm.call_count == 3

    def test_timeout_recovers_if_providers_wakes_up(self) -> None:
        llm = FakeLLM([TimeoutError("抖了一下"), _step_payload()])
        out = _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert out.done is False
        assert llm.call_count == 2

    def test_backoff_delays_are_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("secpt.llms.client.time.sleep", sleeps.append)
        llm = FakeLLM([TimeoutError("t")] * 3)
        with pytest.raises(LlmError):
            LlmClient(llm, backoff_base_s=2.0).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert sleeps == [2.0, 4.0]  # 第 3 次失败后不再睡，直接上抛


class TestFakeLlmScript:
    def test_script_models_dumped_as_json(self) -> None:
        class Echo(BaseModel):
            hi: str

        llm: FakeLLM = FakeLLM([Echo(hi="hey")])
        raw = llm.complete(_MESSAGES, temperature=0.2, timeout_s=60.0)
        assert json.loads(raw) == {"hi": "hey"}

    def test_script_exhaustion_raises_llm_error(self) -> None:
        llm = FakeLLM([])
        with pytest.raises(LlmError) as info:
            llm.complete(_MESSAGES, temperature=0.2, timeout_s=60.0)
        assert info.value.code == "E_FAKE_SCRIPT_EXHAUSTED"

    def test_callable_script_item_gets_invoked(self) -> None:
        llm: FakeLLM = FakeLLM([lambda: _step_payload()])
        raw = llm.complete(_MESSAGES, temperature=0.2, timeout_s=60.0)
        assert json.loads(cast("str", raw))["thought"] == "先扫端口"

    def test_non_timeout_provider_error_bubbles_untouched(self) -> None:
        llm = FakeLLM([ValueError("provider 内部炸了")])
        with pytest.raises(ValueError, match="provider 内部炸了"):
            _client(llm).structured(_MESSAGES, StepOutput, temperature=0.2)
        assert llm.call_count == 1  # 非 TimeoutError 不退避、不吞、原样冒泡
