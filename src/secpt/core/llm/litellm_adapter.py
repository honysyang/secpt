"""litellm → DeepSeek 适配器。

约束：
- API key 只从环境变量 ``LLMConfig.api_key_env`` 读取（默认 DEEPSEEK_API_KEY），
  不写 YAML / 不传代码参数。
- ``model`` 参数透传为 ``f"{provider}/{model}"``，例如 ``deepseek/deepseek-chat``。
- 所有异常统一包成 :class:`LLMError`。
- 每次调用后自动上报 token 消耗到 TokenTracker（若已绑定）。
"""

from __future__ import annotations

import asyncio
import json
import os

# 禁用远程价格表拉取（内网环境会超时，必须在 litellm 导入前设置）
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from typing import Any

import structlog

from secpt.config.models import LLMConfig
from secpt.core.llm.base import BaseLLM, LLMError, LLMMessage

logger = structlog.get_logger(__name__)


class LiteLLMAdapter(BaseLLM):
    """生产用 litellm 适配器。"""

    def __init__(self, config: LLMConfig) -> None:
        super().__init__()
        self._config = config
        raw_key = os.environ.get(config.api_key_env)
        if not raw_key:
            raise LLMError(
                f"缺少环境变量 {config.api_key_env}，"
                f"请先 export {config.api_key_env}=<你的 DeepSeek API key>"
            )
        self._api_key: str = raw_key

    async def acompletion(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMMessage:
        import litellm  # 延迟导入，环境变量已在模块级设置

        # 延迟绑定，便于单测 monkeypatch
        model_full = f"{self._config.provider}/{model or self._config.planning_model}"
        temperature_val = temperature if temperature is not None else self._config.temperature
        max_tokens_val = max_tokens if max_tokens is not None else self._config.max_tokens

        payload_messages: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "tool":
                # 透传真实 tool_call_id：占位符会让多轮工具调用的会话被 API 400 拒绝
                entry["tool_call_id"] = m.tool_call_id or "call_unknown"
            payload_messages.append(entry)

        # 组装 tools 参数
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools

        try:
            resp = await litellm.acompletion(
                model=model_full,
                messages=payload_messages,
                api_key=self._api_key,
                temperature=temperature_val,
                max_tokens=max_tokens_val,
                timeout=self._config.timeout_s,
                **kwargs,
            )
        except litellm.RateLimitError as exc:
            raise LLMError(f"LLM rate limited: {exc}") from exc
        except litellm.APIError as exc:
            raise LLMError(f"LLM API error: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise LLMError(f"LLM request timeout ({self._config.timeout_s}s)") from exc
        except Exception as exc:  # noqa: BLE001 - 边界统一收敛
            raise LLMError(f"LLM unexpected error: {exc}") from exc

        # litellm 响应与 OpenAI 兼容：choices[0].message
        try:
            choice = resp.choices[0]
            msg = choice.message
            content = getattr(msg, "content", None) or ""
            tool_calls: list[dict[str, Any]] = []
            for tc in getattr(msg, "tool_calls", None) or []:
                func = getattr(tc, "function", None)
                if func is None:
                    continue
                name = getattr(func, "name", None)
                if not name:
                    continue
                raw_args = getattr(func, "arguments", None) or "{}"
                try:
                    args_parsed = json.loads(raw_args)
                except (TypeError, ValueError):
                    args_parsed = {"_raw": raw_args}
                tool_calls.append({"name": name, "args": args_parsed})
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMError(f"无法解析 litellm 响应: {exc}") from exc

        # 上报 token 消耗
        usage = getattr(resp, "usage", None)
        if usage is not None:
            prompt_tok = getattr(usage, "prompt_tokens", 0) or 0
            completion_tok = getattr(usage, "completion_tokens", 0) or 0
            # 从 model_full 提取短模型名（如 "deepseek/deepseek-chat" → "deepseek-chat"）
            short_model = model_full.split("/", 1)[-1] if "/" in model_full else model_full
            self._report_tokens(short_model, prompt_tok, completion_tok)
            logger.debug(
                "llm.tokens",
                model=short_model,
                prompt=prompt_tok,
                completion=completion_tok,
            )

        logger.debug(
            "llm.acompletion.ok",
            model=model_full,
            input_messages=len(payload_messages),
            output_chars=len(content),
            tool_calls=len(tool_calls) if tool_calls else 0,
        )
        return LLMMessage(role="assistant", content=content, tool_calls=tool_calls)
