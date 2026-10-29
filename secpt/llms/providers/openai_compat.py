"""openai 兼容 provider（M4-1：真实 LLM 接线——R8 接口层唯一外部依赖档）。

设计口径：
- 只实现 :class:`~secpt.llms.client.LlmProvider` 的 ``complete`` 原语：schema
  解析、带错重试、超时指数退避全是协议层（LlmClient）的纪律，这里不重复。
  超时以 ``TimeoutError`` 冒泡交协议层退避，耗尽后统一上抛 E_TIMEOUT_LLM。
- 非流式 MVP（``stream=false``）：消费方是协议层的 pydantic 解析器，流式分片
  对结构化输出零收益，只添复杂度。
- HTTP 错误 fail-fast 分层，provider 层不重试（重试语义只属协议层的超时退避）：
  401/403 → E_LLM_AUTH；429 → E_LLM_RATE_LIMIT；其余 4xx/5xx → E_LLM_HTTP；
  连接失败 → E_LLM_CONNECT；响应体不合法 → E_LLM_RESPONSE。
- 密钥卫生：Authorization 只进请求头；异常 context 携带的响应体片段先脱敏
  （密钥出现即替换 ``***``），保证 E_LLM_* 落 audit/log 时密钥不外泄。
- 传输用标准库 urllib，零新增依赖：接口层是全项目唯一的外部 LLM I/O，
  依赖面越小，供应链与攻击面越小。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from secpt.errors import LlmError
from secpt.llms.client import Message

_REDACTED = "***"


class OpenAICompatProvider:
    """openai 兼容 chat-completions provider（DeepSeek/vLLM/Ollama 等同协议皆可）。"""

    def __init__(
        self, *, base_url: str, api_key: str, model: str, error_body_chars: int = 200
    ) -> None:
        """``base_url`` 是服务根（如 ``https://api.deepseek.com/v1``），本类自动拼
        ``/chat/completions``；``error_body_chars`` 限异常 context 里响应体片段
        长度，防大响应体把 audit/log 撑爆。"""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._error_body_chars = error_body_chars

    def complete(self, messages: Sequence[Message], *, temperature: float, timeout_s: float) -> str:
        """同步补全一次（非流式）；超时抛 ``TimeoutError`` 交协议层退避。"""
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except TimeoutError as exc:  # socket.timeout 自 3.10 起即 TimeoutError 别名
            raise TimeoutError(f"LLM 服务响应超时（timeout_s={timeout_s}s）") from exc
        except OSError as exc:
            # URLError/连接拒绝/半途断连等都归连接失败——fail-fast，不重试。
            raise LlmError(
                "LLM 服务连接失败", code="E_LLM_CONNECT", context={"reason": str(exc)}
            ) from exc
        return self._extract_content(body)

    def _http_error(self, exc: urllib.error.HTTPError) -> LlmError:
        """HTTP 状态码 → 分层 LlmError；响应体片段截断 + 脱敏后进 context。"""
        snippet = self._redact(
            exc.read(self._error_body_chars + 1).decode("utf-8", errors="replace")
        )[: self._error_body_chars]
        if exc.code in (401, 403):
            code = "E_LLM_AUTH"
        elif exc.code == 429:
            code = "E_LLM_RATE_LIMIT"
        else:
            code = "E_LLM_HTTP"
        return LlmError(
            f"LLM 服务返回 HTTP {exc.code}",
            code=code,
            context={"status": exc.code, "body": snippet},
        )

    def _extract_content(self, body: str) -> str:
        """响应体 → content 字符串：任何形状不合法都显式 E_LLM_RESPONSE，不猜。"""
        try:
            data: Any = json.loads(body)  # Any：LLM 响应边界（开发文档 §6.1 豁免点）
        except json.JSONDecodeError as exc:
            raise LlmError(
                "LLM 响应不是合法 JSON",
                code="E_LLM_RESPONSE",
                context={"body": self._redact(body)[: self._error_body_chars]},
            ) from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(
                "LLM 响应缺 choices[0].message.content",
                code="E_LLM_RESPONSE",
                context={"body": self._redact(body)[: self._error_body_chars]},
            ) from exc
        if not isinstance(content, str):
            raise LlmError(
                "LLM 响应 content 不是字符串",
                code="E_LLM_RESPONSE",
                context={"got_type": type(content).__name__},
            )
        return content

    def _redact(self, text: str) -> str:
        """密钥脱敏：片段可能进异常 context（→ audit/log），密钥绝不外泄。"""
        if self._api_key:
            text = text.replace(self._api_key, _REDACTED)
        return text


__all__ = ["OpenAICompatProvider"]
