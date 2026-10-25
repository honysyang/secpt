"""LLM 客户端基础设施（ARCHITECTURE §8 L1：parse_json / load_client）。

LLMClient 协议定义在 ports.py（坑 A2：runtime 只依赖 ports，不依赖本模块）；
本模块是唯一的具体实现装载点：MockLLM 无网络、无 API key，按 phase 排队
注入脚本，耗尽后重复最后一条——S9/S10 的重试路径测试全靠它。

api_key 纪律：complete() 不得在任何日志 / __repr__ / 异常信息里泄漏 key。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from pa.errors import ModelCallError, ModelParseError
from pa.models import Decision

_JSON_BLOCK = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(raw: str, schema: type | None = None) -> Any:
    """模型输出 → JSON 对象 → schema 校验（ARCHITECTURE §8 L1 签名）。

    坑 A1：Decision 是判别联合，必须走 TypeAdapter(Decision).validate_python，
    不能直接 model_validate。提取宽容（```json 块 / 首个 {...}），解析或
    校验失败一律抛 ModelParseError——与"调用失败"（ModelCallError）区分。
    """
    text = raw.strip()
    m = _JSON_BLOCK.match(text)
    if m:
        text = m.group(1).strip()
    elif not text.startswith("{"):
        m = _JSON_OBJ.search(text)
        if m is None:
            raise ModelParseError(f"模型输出里没有 JSON 对象: {text[:80]!r}")
        text = m.group(0)
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise ModelParseError(f"模型输出不是合法 JSON: {e}") from e
    if schema is None:
        return obj
    try:
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_validate(obj)
        return TypeAdapter(schema).validate_python(obj)
    except ValidationError as e:
        raise ModelParseError(
            f"模型输出 schema 校验失败: {e.errors()[:2]}") from e


def complete_retry(client, *, retries: int = 1, **kw) -> str:
    """调用 + 重试：ModelCallError / ModelParseError 各给 retries 次机会，
    仍失败则抛最后一次的异常（S9/S10 的重试路径）。"""
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            return client.complete(**kw)
        except (ModelCallError, ModelParseError) as e:
            last = e
    raise last  # type: ignore[misc]


class MockLLM:
    """无网络、无 key 的脚本化客户端。

    script: {phase: [原始响应串, ...]}（dict 或 JSON 串）。按 phase 排队
    消费，耗尽后重复该 phase 的最后一条。phase 从未注入 → ModelCallError
    （静默编造决策比重试失败更糟）。支持三种注入：正常 JSON / 畸形 JSON /
    合法 JSON 但 schema 不合法（如缺 kind）——后两种由 parse_json 判为
    ModelParseError。
    """

    def __init__(self, script: dict | str | None = None) -> None:
        if isinstance(script, str):
            try:
                script = json.loads(script)
            except ValueError as e:
                raise ModelCallError(f"MockLLM 脚本不是合法 JSON: {e}") from e
        self._queues: dict[str, list[str]] = {k: list(v)
                                              for k, v in (script or {}).items()}
        self._last: dict[str, str] = {}
        self.calls: list[dict] = []          # 调用记录（审计用，无敏感信息）

    def complete(self, *, system: str, user: str, schema: Any,
                 phase: str, budget_tokens: int | None = None) -> str:
        self.calls.append({"phase": phase, "system": system, "user": user})
        q = self._queues.get(phase)
        if q:
            item = q.pop(0)
            self._last[phase] = item
        elif phase in self._last:
            item = self._last[phase]             # 耗尽后重复最后一条
        else:
            raise ModelCallError(f"MockLLM: phase {phase!r} 没有注入脚本")
        return item

    def parse(self, raw: str, schema: Any) -> Any:
        return parse_json(raw, schema)

    def estimate_tokens(self, text: str) -> int:
        """纯函数（裁定）：同一输入同值，对 len(text) 单调不减。不累计。"""
        return max(1, len(text) // 4)

    def __repr__(self) -> str:
        return f"MockLLM(phases={sorted(self._queues)})"


class ApiLLM:
    """OpenAI 兼容端点的最小真实客户端（v0 默认走 Mock，本类仅供真接）。

    api_key 只存在私有属性里：__repr__ 不含，异常信息只带异常类型名。
    """

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 60.0) -> None:
        self._base_url = base_url or os.environ.get("SECPT_BASE_URL", "")
        self._api_key = api_key or os.environ.get("SECPT_API_KEY", "")
        self._model = model or os.environ.get("SECPT_MODEL", "")
        # SECPT_FALLBACK_MODEL：降级链用（S10 接线），此处仅冻结取值
        self._fallback_model = os.environ.get("SECPT_FALLBACK_MODEL", "")
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"ApiLLM(base_url={self._base_url!r}, model={self._model!r})"

    def complete(self, *, system: str, user: str, schema: Any,
                 phase: str, budget_tokens: int | None = None) -> str:
        if not self._base_url or not self._api_key:
            raise ModelCallError(
                "LLM 客户端未配置：需要 SECPT_BASE_URL / SECPT_API_KEY")
        try:
            with httpx.Client(timeout=self._timeout) as c:
                r = c.post(self._base_url,
                           headers={"Authorization": f"Bearer {self._api_key}"},
                           json={"model": self._model,
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}]})
                r.raise_for_status()
                data = r.json()
            return str(data["choices"][0]["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            # 只带异常类型名，绝不带请求内容（含 key）
            raise ModelCallError(f"LLM 调用失败: {type(e).__name__}") from e

    def parse(self, raw: str, schema: Any) -> Any:
        return parse_json(raw, schema)

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def load_client(mock_script: dict | str | None = None):
    """装载 LLMClient（ARCHITECTURE §8 L1 签名）：显式 mock_script 优先，
    其次 SECPT_LLM_MOCK 环境变量，否则真实客户端（读 SECPT_BASE_URL /
    SECPT_API_KEY / SECPT_MODEL / SECPT_FALLBACK_MODEL）。"""
    if mock_script is not None:
        return MockLLM(mock_script)
    env_mock = os.environ.get("SECPT_LLM_MOCK")
    if env_mock:
        return MockLLM(env_mock)
    return ApiLLM()
