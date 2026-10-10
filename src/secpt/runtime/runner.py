"""顶层 run() 编排：配置 → LLM → Registry → GraphStore → run_react。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from secpt.config.settings import get_settings
from secpt.core.graph.factory import get_graph_store
from secpt.core.llm.factory import get_llm
from secpt.core.react.runner import ReactResult, run_react
from secpt.core.tools.registry import build_registry

logger = structlog.get_logger(__name__)


@dataclass
class RunResult:
    """一次 run() 的完整结果。"""

    answer: str
    steps: int
    exhausted: bool
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    graph_backend: str = ""


async def run(
    target: str,
    *,
    task: str | None = None,
    max_steps: int | None = None,
    fake: bool = False,
) -> RunResult:
    """对 ``target`` 执行一次 ReAct 渗透任务。

    :param target:    目标（host / url / domain）
    :param task:      自定义任务描述；默认生成扫描任务
    :param max_steps: 覆盖 ReAct 最大步数
    :param fake:      True 时用 FakeLLM（dry-run，无 key 也可跑）
    """
    settings = get_settings()

    # 1. LLM
    if fake:
        from secpt.core.llm import FakeLLM, LLMMessage

        llm = FakeLLM(
            script=[
                LLMMessage(role="assistant", content="ACTION: nmap -p 22,80,443 -T4 " + target),
                LLMMessage(
                    role="assistant",
                    content="ANSWER: 目标 " + target + " 扫描完成（FakeLLM dry-run）",
                ),
            ]
        )
    else:
        llm = get_llm(settings.llm)

    # 2. 工具注册表（按白名单）
    reg = build_registry(settings.tools.whitelist)

    # 3. 图存储
    store = get_graph_store(settings.graph)

    # 4. 任务描述
    if task is None:
        task = f"对目标 {target} 进行侦察：先用 dig 查 DNS（如适用），再用 nmap 扫开放端口，最后用 curl 探测 HTTP 服务。汇总发现。"

    # 5. 跑 ReAct
    steps = max_steps or settings.react.max_steps
    logger.info(
        "run.start", target=target, task=task[:100], fake=fake, graph_backend=settings.graph.backend
    )
    try:
        result: ReactResult = await run_react(
            llm=llm,
            tools=reg,
            task=task,
            max_steps=steps,
            store=store,
        )
    finally:
        await store.close()

    logger.info(
        "run.done", answer=result.answer[:200], steps=result.steps, exhausted=result.exhausted
    )
    return RunResult(
        answer=result.answer,
        steps=result.steps,
        exhausted=result.exhausted,
        trajectory=result.trajectory,
        graph_backend=settings.graph.backend,
    )


def run_sync(target: str, **kwargs: Any) -> RunResult:
    """同步壳。"""
    return asyncio.run(run(target, **kwargs))
