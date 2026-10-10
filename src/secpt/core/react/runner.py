"""最小 ReAct 循环。

协议：``ACTION: <tool> <args>`` / ``ANSWER: <text>``（文本协议）。

trace：
- 每步把 assistant content 与 observation 追加到 messages
- trajectory 记录结构化事件（action / observation / finish / exhausted / format_error）
- 全程 structlog 记录 action/observation

可选接图存储（``store``）：把每步 action / observation 落成实体/边/证据，
实现最廉价的反循环（同 tool+args 不再执行）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from secpt.core.graph.base import GraphStore
from secpt.core.llm.base import BaseLLM, LLMMessage
from secpt.core.react.prompts import DEFAULT_SYSTEM_PROMPT
from secpt.core.tools.base import Registry

logger = structlog.get_logger(__name__)


_ACTION_RE = re.compile(r"^ACTION:\s*(\w+)\s*(.*)$", re.IGNORECASE | re.DOTALL)
_ANSWER_RE = re.compile(r"^ANSWER:\s*(.+)$", re.IGNORECASE | re.DOTALL)


@dataclass
class ReactResult:
    """ReAct 运行结果。"""

    answer: str
    steps: int
    exhausted: bool
    trajectory: list[dict[str, Any]] = field(default_factory=list)


def _parse_action(resp: LLMMessage) -> tuple[Literal["answer", "action", "none"], str | None, str | None]:
    """双轨解析：优先 tool_calls（结构化），兜底 ACTION/ANSWER 文本。

    :return: ``("answer", text, None)`` / ``("action", name, args_json_or_raw)`` / ``("none", None, None)``
    """
    # 优先结构化 tool_calls
    if resp.tool_calls:
        first = resp.tool_calls[0]
        name = first.get("name")
        args = first.get("args") or {}
        if name:
            try:
                args_str = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args_str = str(args)
            return ("action", name, args_str)

    # 兜底文本协议
    content = (resp.content or "").strip()
    m_answer = _ANSWER_RE.match(content)
    if m_answer:
        return ("answer", m_answer.group(1).strip(), None)
    m_action = _ACTION_RE.match(content)
    if m_action:
        name = m_action.group(1)
        args = m_action.group(2).strip()
        return ("action", name, args)
    return ("none", None, None)


def _build_initial_messages(task: str, system_prompt: str, tools: Registry) -> list[LLMMessage]:
    tool_list = ", ".join(tools.names())
    user_content = (
        f"任务：{task}\n"
        f"可用工具：{tool_list}\n\n"
        "请按系统提示的格式输出 ACTION 或 ANSWER。"
    )
    return [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_content),
    ]


async def _check_and_write_action(
    store: GraphStore,
    task_eid: str,
    action_eid: str,
    tool_name: str,
    tool_args: str,
) -> str | None:
    """写入 action 实体 + performed 边；若 (tool, args) 已存在则返回重复 observation，否则 None。

    通过比对已存在 action 实体的 props 判断 (tool, args) 是否重复。
    """
    # 检查是否已有相同 (tool, args) 的 action
    existing_neighbors = await store.neighbors(task_eid)
    for nb in existing_neighbors:
        if nb.get("etype") != "action":
            continue
        nb_props = nb.get("props") or {}
        if nb_props.get("tool") == tool_name and nb_props.get("args") == tool_args:
            return "Error: 该动作已执行过，请换工具或给出 ANSWER。"

    await store.upsert_entity(action_eid, "action", {"tool": tool_name, "args": tool_args})
    await store.add_edge(task_eid, action_eid, "performed")
    logger.info(
        "graph.write",
        kind="action",
        eid=action_eid,
        task_eid=task_eid,
        tool=tool_name,
        args=tool_args,
    )
    return None


async def run_react(
    *,
    llm: BaseLLM,
    tools: Registry,
    task: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    max_steps: int = 8,
    store: GraphStore | None = None,
) -> ReactResult:
    """跑一个最小 ReAct 循环。

    :param llm:         任意 :class:`BaseLLM` 实现（FakeLLM / LiteLLMAdapter）
    :param tools:       工具注册表
    :param task:        任务描述（进入第一条 user 消息）
    :param system_prompt: 默认用 :data:`DEFAULT_SYSTEM_PROMPT`
    :param max_steps:   最大步数，耗尽则 ``exhausted=True``
    :param store:       可选图存储；提供时把每步 action/observation 落图
    """
    messages = _build_initial_messages(task, system_prompt, tools)
    trajectory: list[dict[str, Any]] = []

    # 任务根实体（有 store 时建）
    task_eid: str | None = None
    if store is not None:
        task_hash = hashlib.sha256(task.encode("utf-8")).hexdigest()[:12]
        task_eid = f"task.{task_hash}"
        await store.upsert_entity(task_eid, "task", {"task": task})
        logger.info("graph.write", kind="entity", eid=task_eid, etype="task")

    for step in range(max_steps):
        step_index = step + 1
        resp = await llm.acompletion(messages)
        content = (resp.content or "").strip()
        messages.append(LLMMessage(role="assistant", content=content, tool_calls=resp.tool_calls))

        kind, a1, a2 = _parse_action(resp)

        if kind == "answer":
            answer = (a1 or "").strip()
            trajectory.append({"type": "finish", "step": step_index, "answer": answer})
            logger.info("react.finish", step=step_index, answer=answer)
            return ReactResult(answer=answer, steps=step_index, exhausted=False, trajectory=trajectory)

        if kind == "action":
            tool_name = a1 or ""
            tool_args = (a2 or "").strip()
            action_eid = f"act.{step_index}.{tool_name}"

            # ---- 图写入：action 实体 + performed 边（含反循环检查） ----
            if store is not None and task_eid is not None:
                dup_obs = await _check_and_write_action(store, task_eid, action_eid, tool_name, tool_args)
                if dup_obs is not None:
                    trajectory.append({
                        "type": "action",
                        "step": step_index,
                        "tool": tool_name,
                        "args": tool_args,
                        "observation": dup_obs,
                        "deduplicated": True,
                    })
                    messages.append(LLMMessage(role="user", content=f"观察：{dup_obs}"))
                    continue

            tool = tools.get(tool_name)
            if tool is None:
                observation = f"Error: unknown tool {tool_name}"
                logger.warning(
                    "react.action.unknown_tool",
                    step=step_index,
                    tool=tool_name,
                    args=tool_args,
                )
            else:
                logger.info(
                    "react.action",
                    step=step_index,
                    tool=tool_name,
                    args=tool_args,
                )
                try:
                    observation = await tool.run(tool_args)
                except Exception as exc:  # noqa: BLE001 - 防御
                    observation = f"Error: {exc}"
                logger.info(
                    "react.observation",
                    step=step_index,
                    tool=tool_name,
                    observation=observation[:200],
                )

                # ---- 图写入：observation 落证据（确定性执行 → 最高档） ----
                if store is not None:
                    evidence_id = await store.add_evidence(
                        action_eid, observation, trust_tier="deterministic"
                    )
                    logger.info(
                        "graph.write",
                        kind="evidence",
                        evidence_id=evidence_id,
                        eid=action_eid,
                        trust_tier="deterministic",
                    )

            trajectory.append({
                "type": "action",
                "step": step_index,
                "tool": tool_name,
                "args": tool_args,
                "observation": observation,
            })
            # 文本协议下以 user 角色回发 observation，规避 DeepSeek/OpenAI
            # 对 role="tool" 必须紧跟 tool_calls 的校验
            messages.append(LLMMessage(role="user", content=f"观察：{observation}"))
            continue

        # kind == "none"：格式错误，让 LLM 自我纠正
        observation = "Error: 格式错误，请用 ACTION: 或 ANSWER:"
        logger.warning(
            "react.format_error",
            step=step_index,
            raw_content=content[:200],
        )
        trajectory.append({
            "type": "format_error",
            "step": step_index,
            "raw_content": content,
            "observation": observation,
        })
        messages.append(LLMMessage(role="user", content=f"观察：{observation}"))

    logger.warning("react.exhausted", max_steps=max_steps)
    trajectory.append({"type": "exhausted", "step": max_steps})
    return ReactResult(
        answer="(达到最大步数，未给出结论)",
        steps=max_steps,
        exhausted=True,
        trajectory=trajectory,
    )


__all__ = ["ReactResult", "run_react"]
