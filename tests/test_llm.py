"""S7 验收：MockLLM 三种注入、TypeAdapter 校验、错误分类、key 不泄漏。

验收条目：
- 无网络、无 API key，Mock 返回的 Decision 能被 TypeAdapter(Decision) 通过；
- 畸形 JSON → ModelParseError；重试 1 次仍失败 → 第二次抛；
- 合法 JSON 但 schema 不合法（缺 kind）→ ModelParseError；
- 客户端异常 → ModelCallError（不被误判成解析失败）；
- repr / 异常信息里搜不到 api_key。
"""
from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter

from pa.errors import ModelCallError, ModelParseError
from pa.infra.llm import ApiLLM, MockLLM, complete_retry, load_client, parse_json
from pa.models import Decision

_GOOD = json.dumps({
    "kind": "call_tool", "tool": "probe",
    "args": {"target": "10.0.0.5", "port": 80},
    "expect": "看到 open 或 closed"}, ensure_ascii=False)
_SCHEMA_BAD = json.dumps({"tool": "probe", "args": {}})   # 缺 kind
_MALFORMED = '{"kind": "call_tool", ...'


def _complete(client, phase="decide"):
    return client.complete(system="s", user="u", schema=Decision, phase=phase)


def test_mock_decision_passes_type_adapter():
    """验收一：无网络无 key，Mock 输出过 TypeAdapter(Decision)（坑 A1）。"""
    client = MockLLM({"decide": [_GOOD]})
    raw = _complete(client)
    decision = TypeAdapter(Decision).validate_python(json.loads(raw))
    assert decision.kind == "call_tool" and decision.tool == "probe"
    # client.parse 走同一条 TypeAdapter 路径
    assert client.parse(raw, Decision).kind == "call_tool"


def test_malformed_json_raises_parse_error():
    """验收二：畸形 JSON → ModelParseError。"""
    with pytest.raises(ModelParseError):
        parse_json(_MALFORMED, Decision)
    client = MockLLM({"decide": [_MALFORMED, _MALFORMED]})
    with pytest.raises(ModelParseError):            # 第 1 次
        client.parse(_complete(client), Decision)
    with pytest.raises(ModelParseError):            # 重试 1 次仍失败 → 第 2 次抛
        client.parse(_complete(client), Decision)
    assert len(client.calls) == 2


def test_complete_retry_exhausts():
    """complete_retry(retries=1)：两次尝试后抛最后一次的异常。

    注意 complete 只返回原始文本，解析在 parse——重试包装的是"取数"这一
    步；这里用注入异常的客户端验证 complete 侧的重试语义。"""
    class FlakyCall(MockLLM):
        def __init__(self):
            super().__init__({"decide": [_GOOD]})
            self._left = 2

        def complete(self, **kw):
            if self._left:
                self._left -= 1
                raise ModelCallError("transient")
            return super().complete(**kw)

    client = FlakyCall()
    with pytest.raises(ModelCallError, match="transient"):
        complete_retry(client, retries=1, system="s", user="u",
                       schema=Decision, phase="decide")
    assert len(client.calls) == 0                # 两次都在 complete 阶段失败
    # 第三次成功（重试路径的正面情形）
    assert complete_retry(client, retries=1, system="s", user="u",
                          schema=Decision, phase="decide") == _GOOD


def test_schema_invalid_raises_parse_error():
    """验收三：合法 JSON 但缺 kind → ModelParseError。"""
    with pytest.raises(ModelParseError):
        parse_json(_SCHEMA_BAD, Decision)
    client = MockLLM({"decide": [_SCHEMA_BAD]})
    with pytest.raises(ModelParseError):
        client.parse(_complete(client), Decision)


def test_client_error_is_call_error_not_parse():
    """验收四：调用失败是 ModelCallError，不被误判成解析失败。"""

    class BrokenLLM:
        def complete(self, **kw):
            raise ModelCallError("connection refused")

        def parse(self, raw, schema):
            return parse_json(raw, schema)

    with pytest.raises(ModelCallError, match="connection refused"):
        complete_retry(BrokenLLM(), retries=1, system="s", user="u",
                       schema=Decision, phase="decide")


def test_no_api_key_leak(monkeypatch):
    """验收五：repr 与异常信息里都搜不到 key。"""
    monkeypatch.setenv("SECPT_API_KEY", "sk-test-SECRETLEAK")
    monkeypatch.setenv("SECPT_BASE_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.delenv("SECPT_LLM_MOCK", raising=False)
    client = load_client()
    assert isinstance(client, ApiLLM)
    assert "sk-" not in repr(client)
    with pytest.raises(ModelCallError) as ei:
        _complete(client)                       # 127.0.0.1:9 拒绝连接，快速失败
    assert "sk-" not in str(ei.value)
    assert "SECRETLEAK" not in str(ei.value)


def test_estimate_tokens_pure():
    """必改 1：纯函数——幂等，对 len(text) 单调不减，不累计。"""
    client = MockLLM({"decide": [_GOOD]})
    f = client.estimate_tokens
    assert f("abcdefghij") == f("abcdefghij")
    assert f("x" * 100) <= f("x" * 200)
    assert f("") == f("") and f("") >= 1


def test_phase_queue_exhausted_repeats_last():
    client = MockLLM({"decide": ["first", "second"]})
    assert _complete(client) == "first"
    assert _complete(client) == "second"
    assert _complete(client) == "second"        # 耗尽后重复最后一条
    assert _complete(client) == "second"


def test_uninjected_phase_raises_call_error():
    client = MockLLM({"decide": [_GOOD]})
    with pytest.raises(ModelCallError, match="no_script|没有注入"):
        _complete(client, phase="replan")
