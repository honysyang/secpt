"""脚本化假 LLM：contract test 与 M1 离线演示的唯一 provider。

为什么是脚本而不是小模型：开发文档钉死"纯代码视角可测——FakeLLM 可脚本化、
任何强依赖真实 LLM 的路径不许进单测"。本类就是那句契约的执行者：
调用方按 W 系列用例把"第 N 次调用返回什么"排进队列（合法 JSON / 坏 JSON /
任意异常），协议层（LlmClient）看到的与真 provider 无异。

同时它作为 ``LLM_PROVIDER=mock`` 的出厂 provider：默认脚本示意一次最短可跑通
的响应，让 ``secpt run`` 在零网络环境下全流程演示（W1-7 落历程、e2e 在 W2-3）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel

from secpt.errors import LlmError
from secpt.llms.client import Message

ScriptItem = str | Exception | BaseModel | Callable[[], str | Exception | BaseModel]


class FakeLLM:
    """按脚本逐次吐词的 provider；每次调用原样记录，供断言（contract test 主战场）。

    脚本项语义：
    - ``str``：原样作为模型文本返回（通常是一段 JSON）；
    - ``pydantic BaseModel``：dump 成紧凑 JSON 返回（测试里写模型比手写 JSON 顺手）；
    - ``Exception``：原样抛出（``TimeoutError`` 走协议层退避，``ValueError`` 直接冒泡）；
    - ``Callable``：返回上述任一，可自定义基于已发生调用次数的动态脚本。
    """

    def __init__(self, script: Sequence[ScriptItem] = ()) -> None:
        self._script: list[ScriptItem] = list(script)
        self._cursor = 0
        self.calls: list[list[Message]] = []

    def queue(self, item: ScriptItem) -> None:
        """追加一条脚本（测试可在运行中途续排队，模拟"第 N 步才失败"）。"""
        self._script.append(item)

    @property
    def remaining(self) -> int:
        """尚未消耗的脚本条数（测试断言：脚本应正好耗尽，不多不少）。"""
        return len(self._script) - self._cursor

    @property
    def call_count(self) -> int:
        """协议层到 provider 的真实调用次数（退避/重试都会 +1）。"""
        return len(self.calls)

    def complete(self, messages: Sequence[Message], *, temperature: float, timeout_s: float) -> str:
        """实现 :class:`~secpt.llms.client.LlmProvider`；脚本耗尽即显式报错，不给垃圾。"""
        self.calls.append(list(messages))
        item = self._next_item()
        if callable(item):
            item = item()
        if isinstance(item, BaseModel):
            item = item.model_dump_json()
        if isinstance(item, Exception):
            raise item
        return item

    def _next_item(self) -> ScriptItem:
        try:
            item = self._script[self._cursor]
        except IndexError as exc:
            raise LlmError(
                "FakeLLM 脚本耗尽：请检查测试脚本长度与调用次数是否匹配",
                code="E_FAKE_SCRIPT_EXHAUSTED",
                context={"consumed": self._cursor},
            ) from exc
        self._cursor += 1
        return item


__all__ = ["FakeLLM", "ScriptItem"]
