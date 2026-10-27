"""流式输出：SSE / WebSocket 实时推送。

支持：
- Server-Sent Events (SSE)：单向推送，轻量级
- WebSocket：双向通信，支持交互
- 异步生成器：逐 token 推送
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class StreamEventType(StrEnum):
    """流事件类型。"""

    START = "start"  # 流开始
    TOKEN = "token"  # 新 token
    TOOL_CALL = "tool_call"  # 工具调用
    TOOL_RESULT = "tool_result"  # 工具结果
    THINKING = "thinking"  # 思考过程
    ERROR = "error"  # 错误
    END = "end"  # 流结束


@dataclass
class StreamEvent:
    """流事件。"""

    type: StreamEventType
    data: Any
    timestamp: float = field(default_factory=time.time)
    sequence: int = 0  # 序号，用于排序


class EventStream:
    """事件流：管理订阅者和事件分发。"""

    def __init__(self) -> None:
        """初始化事件流。"""
        self._subscribers: list[asyncio.Queue] = []
        self._sequence = 0
        self._closed = False

    async def publish(self, event_type: StreamEventType, data: Any) -> None:
        """发布事件。

        Args:
            event_type: 事件类型
            data: 事件数据
        """
        if self._closed:
            return

        self._sequence += 1
        event = StreamEvent(
            type=event_type,
            data=data,
            sequence=self._sequence,
        )

        # 分发到所有订阅者
        for queue in self._subscribers:
            try:
                await queue.put(event)
            except asyncio.QueueFull:
                pass  # 跳过满的队列

    async def subscribe(self, max_buffer: int = 100) -> AsyncGenerator[StreamEvent, None]:
        """订阅事件流。

        Args:
            max_buffer: 缓冲区大小

        Yields:
            流事件
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=max_buffer)
        self._subscribers.append(queue)

        try:
            while True:
                event = await queue.get()
                yield event

                if event.type == StreamEventType.END:
                    break
        finally:
            self._subscribers.remove(queue)

    def close(self) -> None:
        """关闭流。"""
        self._closed = True


class SSEStream:
    """SSE (Server-Sent Events) 流。

    格式：
    ```
    data: {"type": "token", "data": "Hello", "timestamp": 123}

    data: {"type": "end", "data": null}
    ```
    """

    def __init__(self) -> None:
        """初始化 SSE 流。"""
        self._stream = EventStream()

    async def publish_token(self, token: str) -> None:
        """发布 token。"""
        await self._stream.publish(StreamEventType.TOKEN, token)

    async def publish_thinking(self, thought: str) -> None:
        """发布思考过程。"""
        await self._stream.publish(StreamEventType.THINKING, thought)

    async def publish_tool_call(self, tool_name: str, args: dict) -> None:
        """发布工具调用。"""
        await self._stream.publish(StreamEventType.TOOL_CALL, {
            "name": tool_name,
            "args": args,
        })

    async def publish_tool_result(self, tool_name: str, result: Any) -> None:
        """发布工具结果。"""
        await self._stream.publish(StreamEventType.TOOL_RESULT, {
            "name": tool_name,
            "result": result,
        })

    async def publish_error(self, error: str) -> None:
        """发布错误。"""
        await self._stream.publish(StreamEventType.ERROR, error)

    async def finish(self) -> None:
        """结束流。"""
        await self._stream.publish(StreamEventType.END, None)
        self._stream.close()

    async def stream(self) -> AsyncGenerator[str, None]:
        """生成 SSE 格式的流。

        Yields:
            SSE 格式的字符串（以 \n\n 结尾）
        """
        async for event in self._stream.subscribe():
            data = json.dumps({
                "type": event.type,
                "data": event.data,
                "timestamp": event.timestamp,
                "sequence": event.sequence,
            }, ensure_ascii=False)

            yield f"data: {data}\n\n"

            if event.type == StreamEventType.END:
                break


class WebSocketStream:
    """WebSocket 流：支持双向通信。"""

    def __init__(self) -> None:
        """初始化 WebSocket 流。"""
        self._stream = EventStream()
        self._incoming: asyncio.Queue = asyncio.Queue()  # 接收客户端消息

    async def publish_token(self, token: str) -> None:
        """发布 token。"""
        await self._stream.publish(StreamEventType.TOKEN, token)

    async def publish_thinking(self, thought: str) -> None:
        """发布思考过程。"""
        await self._stream.publish(StreamEventType.THINKING, thought)

    async def finish(self) -> None:
        """结束流。"""
        await self._stream.publish(StreamEventType.END, None)
        self._stream.close()

    async def receive(self) -> Any:
        """接收客户端消息。"""
        return await self._incoming.get()

    async def send(self, message: Any) -> None:
        """发送消息到客户端。"""
        if isinstance(message, str):
            await self._incoming.put(message)
        else:
            await self._incoming.put(json.dumps(message))

    async def stream(self) -> AsyncGenerator[dict, None]:
        """生成 WebSocket 消息流。

        Yields:
            消息字典
        """
        async for event in self._stream.subscribe():
            yield {
                "type": event.type,
                "data": event.data,
                "timestamp": event.timestamp,
                "sequence": event.sequence,
            }

            if event.type == StreamEventType.END:
                break


class StreamingLLM:
    """流式 LLM 客户端：逐 token 输出。"""

    def __init__(self, base_llm: Any) -> None:
        """初始化。

        Args:
            base_llm: 基础 LLM 客户端
        """
        self.base_llm = base_llm

    async def stream_chat(
        self,
        messages: list[dict],
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        """流式对话。

        Args:
            messages: 消息列表
            on_token: 每个 token 的回调

        Returns:
            完整响应
        """
        # 模拟流式输出（实际应该调用 LLM 的流式 API）
        response = await self.base_llm.chat(messages)

        content = response["choices"][0]["message"]["content"]

        # 逐 token 输出
        full_response = ""
        for token in content:
            full_response += token
            if on_token:
                result = on_token(token)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(0.01)  # 模拟延迟

        return full_response


class StreamingExecutionLoop:
    """流式执行循环：边执行边推送事件。"""

    def __init__(self, llm: Any, tools: Any = None) -> None:
        """初始化。

        Args:
            llm: LLM 客户端
            tools: 工具注册表
        """
        self.llm = StreamingLLM(llm)
        self.tools = tools
        self.sse = SSEStream()

    async def run_react_streaming(
        self,
        task: str,
        max_steps: int = 10,
    ) -> str:
        """流式 ReAct 循环。

        Args:
            task: 任务描述
            max_steps: 最大步数

        Returns:
            最终输出
        """
        await self.sse._stream.publish(StreamEventType.START, {"task": task})

        messages = [{"role": "user", "content": task}]
        final_output = ""

        for step in range(max_steps):
            # 流式输出思考过程
            await self.sse.publish_thinking(f"Step {step + 1}: 分析任务...")

            # 调用 LLM（流式）
            response_text = await self.llm.stream_chat(
                messages,
                on_token=self.sse.publish_token,
            )

            messages.append({"role": "assistant", "content": response_text})

            # 检查是否需要工具调用
            if self.tools and "use_tool:" in response_text:
                # 解析工具调用
                tool_name = response_text.split("use_tool:")[1].split()[0]
                await self.sse.publish_tool_call(tool_name, {})

                # 执行工具
                result = f"Tool {tool_name} result"
                await self.sse.publish_tool_result(tool_name, result)

                messages.append({
                    "role": "tool",
                    "content": result,
                })
            else:
                final_output = response_text
                break

        await self.sse.finish()
        return final_output

    async def get_sse_stream(self) -> AsyncGenerator[str, None]:
        """获取 SSE 流。"""
        async for chunk in self.sse.stream():
            yield chunk
