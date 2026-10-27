"""LLM 适配层：统一 OpenAI SDK 为内部 chat() 协议。

ExecutionLoop 期望的协议：
    async def chat(messages, tools) -> dict

OpenAI SDK 返回的对象需要转换为上述 dict 格式。
"""

from __future__ import annotations

from typing import Any

from harness.config import get_settings


class OpenAICompatibleLLM:
    """OpenAI 兼容接口的 LLM 适配器。

    支持：OpenAI / DeepSeek / Moonshot / 其他 OpenAI 兼容服务
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self._base_url = base_url
        self.kwargs = kwargs
        self._client: Any = None

    @property
    def client(self) -> Any:
        """延迟构造 SDK 客户端：与 AnthropicLLM 风格统一，且便于测试注入替身。"""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self._base_url)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """发送聊天请求，返回统一格式。"""
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            **self.kwargs,
        }
        if tools:
            params["tools"] = tools

        response = await self.client.chat.completions.create(**params)

        # 转换为内部协议格式
        message = response.choices[0].message
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": tool_calls,
                }
            }],
            "usage": {
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            },
        }

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """流式聊天：逐段产出文本增量，最终产出与 chat() 相同结构的完整响应。

        事件协议（async generator）：
            {"type": "delta", "content": "增量文本"}
            {"type": "final", "message": {...}, "usage": {...}}   # 收尾必发

        工具调用以增量分片到达（index 标识归属、arguments 跨 chunk 拼接），
        此处聚合完整 tool_calls，调用方无需感知流式细节。
        """
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self.kwargs,
        }
        if tools:
            params["tools"] = tools

        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {"total_tokens": 0}

        stream = await self.client.chat.completions.create(**params)
        async for chunk in stream:
            if chunk.usage:
                usage = {"total_tokens": chunk.usage.total_tokens}
            if not chunk.choices:
                continue  # usage-only 收尾帧
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                yield {"type": "delta", "content": delta.content}
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    acc = tool_acc.setdefault(tc.index, {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            acc["function"]["name"] = tc.function.name
                        if tc.function.arguments:
                            acc["function"]["arguments"] += tc.function.arguments

        yield {
            "type": "final",
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
                "tool_calls": [tool_acc[i] for i in sorted(tool_acc)],
            },
            "usage": usage,
        }


class AnthropicLLM:
    """Anthropic Claude 适配器。"""

    def __init__(
        self,
        model: str,
        api_key: str,
        **kwargs: Any,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.kwargs = kwargs
        self._client: Any = None

    async def _get_client(self) -> Any:
        """延迟初始化客户端。"""
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """发送聊天请求，返回统一格式。"""
        client = await self._get_client()

        # 分离 system 和 user/assistant 消息
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        chat_messages = [m for m in messages if m["role"] != "system"]

        # 转换消息格式
        anthropic_messages = []
        for m in chat_messages:
            role = m["role"]
            if role == "tool":
                # Anthropic 使用不同的 tool_result 格式
                anthropic_messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "content": m["content"]}],
                })
            else:
                anthropic_messages.append({"role": role, "content": m["content"]})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            **self.kwargs,
        }
        if system_msg:
            kwargs["system"] = system_msg

        response = await client.messages.create(**kwargs)

        # 转换为内部协议格式
        content = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content += block.text

        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                }
            }],
            "usage": {
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            },
        }


def build_llm(provider: str | None = None, **kwargs: Any) -> Any:
    """构建 LLM 客户端（适配器模式）。

    Args:
        provider: 提供商名称，默认从配置读取
        **kwargs: 额外参数（temperature、max_tokens 等）

    Returns:
        实现 chat() 协议的 LLM 客户端
    """
    settings = get_settings()
    provider = provider or settings.llm_provider

    # 凭证预检：空 key 时给出指名道姓的错误，
    # 避免 OpenAI SDK 报出误导性的 "OPENAI_API_KEY missing"
    api_key = getattr(settings, f"{provider}_api_key", "")
    if not api_key:
        raise ValueError(
            f"提供商 {provider!r} 缺少 API Key。"
            f"请在项目根 .env 中设置 {provider.upper()}_API_KEY 并重新启动"
        )

    configs = {
        "deepseek": lambda: OpenAICompatibleLLM(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com/v1",
            **kwargs,
        ),
        "openai": lambda: OpenAICompatibleLLM(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            **kwargs,
        ),
        "anthropic": lambda: AnthropicLLM(
            model=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            **kwargs,
        ),
    }

    if provider not in configs:
        raise ValueError(f"Unknown provider: {provider}. Available: {list(configs.keys())}")

    client = configs[provider]()

    # 包一层韧性组件：429/超时/5xx 指数退避重试 + 熔断 + 限流 + 响应缓存。
    # 不包的话这些错误会直接炸到用户面板——组件存在但从未接线。
    from harness.resilience.llm import ResilientLLM

    return ResilientLLM(client)
