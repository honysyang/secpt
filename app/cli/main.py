"""交互式 CLI：基于 harness 的智能助理示例。

支持三种模式：
- single: 单 Agent 模式（ReAct 循环 + 工具调用，流式输出）
- team:   多 Agent 协作模式（Router 路由，单专家执行）
- swarm:  事件驱动集群（Planner 拆意图 → 多 Worker 并行认领执行）

交互命令：
- quit/exit/q: 退出
- mode single/team/swarm: 切换模式
- tools: 查看可用工具
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()


def _create_llm(provider: str | None = None) -> Any:
    """创建真实 LLM 客户端（委托共享引导模块，与 Web 保持一致）。"""
    from app.bootstrap import create_llm

    return create_llm(provider)


def _register_default_tools(registry: Any) -> None:
    """注册内置工具集 + 加载用户自定义脚本工具（tools/ 目录，可选）。"""
    from app.bootstrap import build_tool_registry

    # 共享构造：内置工具 + 脚本工具；CLI 额外做提示与 HITL 挂钩
    built = build_tool_registry(load_script_tools=True)
    registry._tools = built._tools
    console.print(f"[dim]工具: {len(registry._tools)} 个[/dim]")

    # HITL：危险工具（run_command/python_script/write_file 等）执行前人工确认
    registry.approval_hook = _hitl_confirm


async def _hitl_confirm(tool_name: str, arguments: str) -> bool:
    """危险工具审批回调：终端内 y/N 确认（在线程池里跑同步 input，不阻塞事件循环）。"""
    console.print(f"\n[yellow]⚠ 危险工具请求执行: {tool_name}[/yellow]")
    console.print(f"[yellow]  参数: {arguments[:200]}[/yellow]")
    answer = await asyncio.to_thread(console.input, "[yellow]批准执行? [y/N]: [/yellow]")
    return answer.strip().lower() in ("y", "yes")


_BLACKBOARD_SNAPSHOT = Path("snapshots/blackboard.json")


def _restore_blackboard(blackboard: Any) -> None:
    """启动时恢复上次会话的黑板快照（静默降级：无文件/损坏则跳过）。"""
    if not _BLACKBOARD_SNAPSHOT.exists():
        return
    try:
        data = json.loads(_BLACKBOARD_SNAPSHOT.read_text(encoding="utf-8"))
        count = blackboard.restore_state(data)
        console.print(f"[dim]已恢复黑板记忆: {count} 条[/dim]")
    except Exception as e:
        console.print(f"[yellow]⚠ 黑板快照恢复失败（忽略）: {e}[/yellow]")


def _save_blackboard(blackboard: Any) -> None:
    """会话退出时保存黑板快照（swarm 模式的跨会话记忆）。"""
    if blackboard is None:
        return
    try:
        _BLACKBOARD_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        data = blackboard.export_state()
        _BLACKBOARD_SNAPSHOT.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print("[dim]黑板记忆已保存[/dim]")
    except Exception as e:
        console.print(f"[yellow]⚠ 黑板快照保存失败（忽略）: {e}[/yellow]")


async def _run_single_mode(
    user_input: str, llm: Any, tools: Any, history: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """单 Agent 模式：ReAct 循环（LLM 文本增量实时流式输出）。

    Args:
        history: 上一轮 result.messages，传入实现跨轮会话记忆

    Returns:
        本轮完整消息历史（调用方保存，下一轮传回）
    """
    from runtime.loop import ExecutionLoop

    def on_token(text: str) -> None:
        # markup=False/highlight=False：用户内容不被 rich 标记语法污染
        console.print(text, end="", markup=False, highlight=False)

    console.print("\n[bold green]🤖 [/bold green]", end="")
    loop = ExecutionLoop(llm=llm, tools=tools, max_steps=15, on_token=on_token)
    result = await loop.run_react(user_input, history=history)
    console.print()  # 流式结束后换行

    if result.steps:
        console.print("\n[dim]执行步骤:[/dim]")
        for step in result.steps:
            if step["type"] == "thought":
                if not step["content"].strip():
                    continue  # 纯工具调用无思考文本，跳过空 Thought
                console.print(f"  [blue]💭 Thought:[/blue] {step['content'][:60]}...")
            elif step["type"] == "action":
                console.print(f"  [yellow]🔧 Action:[/yellow] {step['tool']}({step['args'][:30]}...)")
            elif step["type"] == "observation":
                # 取首个非空行，避免展示全是空白/响应头前缀
                first = next((l for l in step["content"].splitlines() if l.strip()), "")
                console.print(f"  [green]👀 Observation:[/green] {first[:60]}")

    return result.messages or []


def _load_roles(path: str | Path | None = None) -> Any:
    """加载角色注册表（委托共享引导模块，与 Web 保持一致）。"""
    from app.bootstrap import load_roles

    return load_roles(path)


_task_counter = 0  # 会话内任务序号（仅用于黑板 key 可读性）


async def _run_team_mode(
    user_input: str, llm: Any, tools: Any, session: Any
) -> str:
    """多 Agent 协作模式：Router 路由（角色与专家池会话级构造一次）。"""
    from runtime.agents.collaboration import Router

    router = Router(coordinator=session["coordinator"], sub_agents=session["experts"])

    result = await router.route(user_input)

    console.print(f"\n[dim]路由到: {result.metadata.get('routed_to', 'unknown')}[/dim]")
    for r in result.agent_results:
        console.print(f"  [dim]{r.agent_name}: {r.tokens_used} tokens[/dim]")

    return result.output


async def _run_swarm_mode(
    user_input: str, llm: Any, tools: Any, blackboard: Any, session: Any
) -> str:
    """事件驱动集群模式：Planner 拆意图 → 多 Worker 并行认领执行。

    与 team（Router）的区别：多个智能体真正同时工作，中间发现
    写回共享黑板供后续意图参考，Planner 可依据黑板状态追加意图。
    """
    from runtime.event_driven import EventDrivenEngine

    global _task_counter
    _task_counter += 1

    experts = session["experts"]

    async def execute_intent(intent: Any) -> str | None:
        """意图执行器：按角色路由到真实专家（带工具的 ReAct 智能体，流式输出）。"""
        agent = experts.get(intent.agent_role)
        if agent is None:
            return None  # 无匹配角色 → 回退 Worker 内置的纯 LLM 执行
        console.print(
            f"\n[yellow]▶ [{intent.agent_role}] {intent.description}[/yellow]"
        )
        console.print("[dim]" + "─" * 60 + "[/dim]")

        def on_token(text: str) -> None:
            # 多 Worker 并发时各自流式，块级输出不会字符级交错
            console.print(text, end="", markup=False, highlight=False)

        result = await agent.run(intent.description, on_token=on_token)
        console.print()  # 流式结束换行
        # 结果写回黑板领域层，供后续意图（及其他 Worker）参考
        try:
            await blackboard.write_domain("tasks", f"intent_{intent.id}", result.output)
        except Exception:
            console.print("[dim]⚠ 黑板写入失败（执行不受影响）[/dim]")
        console.print(
            f"[green]✔ [{intent.agent_role}] 完成 ({result.tokens_used} tokens)[/green]"
        )
        return result.output

    # 任务写入黑板全局层（架构一致性：任务本身也是黑板状态）
    await blackboard.write_global(f"task_{_task_counter}", user_input)

    engine = EventDrivenEngine(
        blackboard,
        llm,
        num_workers=2,
        executor=execute_intent,
        available_roles=list(experts.keys()),
    )
    report = await engine.run(task_description=user_input)

    stats = report["frontier_stats"]
    console.print(
        f"\n[dim]集群执行完毕: 完成 {stats.completed} / 失败 {stats.failed} | "
        f"Planner 唤醒 {report['wake_count']} 次[/dim]"
    )

    # 汇总：全文已流式输出，这里只列摘要
    sections: list[str] = []
    for intent in report["intents"]:
        if intent.result:
            first_line = str(intent.result).strip().split("\n", 1)[0][:80]
            sections.append(f"- **[{intent.agent_role}]** {intent.description} → {first_line}")
    if not sections:
        return "（本次没有产生任何执行结果）"
    return "### 各意图执行摘要\n\n" + "\n".join(sections)


async def run_cli() -> None:
    """运行交互式 CLI。"""
    console.print(Panel(
        "[bold blue]Harness 智能助理[/bold blue]\n"
        "输入 'quit' 退出 | 'mode single/team/swarm' 切换模式 | 'tools' 查看工具",
        title="[bold green]启动[/bold green]"
    ))

    # 初始化 LLM
    try:
        llm = _create_llm()
        console.print("[dim]LLM 已就绪[/dim]")
    except Exception as e:
        console.print(f"[red]LLM 初始化失败: {e}[/red]")
        console.print("[yellow]请配置 API Key: cp .env.example .env[/yellow]")
        return

    # 注册工具
    from harness.tools.registry import ToolRegistry
    tools = ToolRegistry()
    _register_default_tools(tools)

    # 会话级构造（一次）：角色注册表 + 全量专家池 + 协调者。
    # 之前每条输入都重建 roles/experts/LLM 客户端，纯浪费
    roles = _load_roles()
    session = {
        "roles": roles,
        "coordinator": roles.create_agent("coordinator", llm),
        "experts": roles.create_experts(llm, shared_tools=tools),
    }

    mode = "single"
    session_history: list[dict[str, Any]] = []  # single 模式跨轮会话记忆
    swarm_blackboard: Any = None  # swarm 模式的会话级共享黑板（跨任务累积发现）
    console.print(f"[dim]当前模式: {mode}[/dim]")

    while True:
        try:
            user_input = console.input(f"\n[bold cyan]👤 [{mode}] 你: [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]再见！[/yellow]")
            break

        if not user_input:
            continue

        # 命令处理
        cmd = user_input.lower()
        if cmd in ("quit", "exit", "q"):
            _save_blackboard(swarm_blackboard)
            console.print("[yellow]再见！[/yellow]")
            break
        elif cmd == "mode single":
            mode = "single"
            console.print("[green]切换到单 Agent 模式[/green]")
            continue
        elif cmd == "mode team":
            mode = "team"
            console.print("[green]切换到多 Agent 协作模式[/green]")
            continue
        elif cmd == "mode swarm":
            mode = "swarm"
            if swarm_blackboard is None:
                from runtime.blackboard import HierarchicalBlackboard
                swarm_blackboard = HierarchicalBlackboard()
                _restore_blackboard(swarm_blackboard)
            console.print("[green]切换到事件驱动集群模式（多 Worker 并行）[/green]")
            continue
        elif cmd == "tools":
            console.print(f"[dim]可用工具: {tools.list_tools()}[/dim]")
            continue

        # 处理输入
        try:
            if mode == "single":
                # 流式输出已在 _run_single_mode 内实时渲染，不再重复打印面板
                session_history = await _run_single_mode(
                    user_input, llm, tools, history=session_history
                )
                continue
            if mode == "swarm":
                # swarm 模式实时打印每个意图的认领/完成状态，最终结果走面板
                response = await _run_swarm_mode(
                    user_input, llm, tools, swarm_blackboard, session
                )
            else:
                response = await _run_team_mode(user_input, llm, tools, session)
        except Exception as e:
            response = f"[red]错误: {e}[/red]"

        console.print(Panel(
            Markdown(response),
            title="[bold green]🤖 助手[/bold green]",
            border_style="green"
        ))


def main() -> None:
    """同步入口。"""
    asyncio.run(run_cli())


if __name__ == "__main__":
    main()
