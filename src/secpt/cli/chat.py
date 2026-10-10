"""secpt chat — 交互式会话 REPL。

不改既有算法与机制，只新增一层对话壳：
- 自然语言即任务，走完整 EngagementRunner（Node 树 + 波前）
- / 开头是内置命令，? 开头是问答，其它一律任务派发
- 会话持有唯一 WorldModel，跨任务复用
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import structlog
import typer

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 会话对象
# ---------------------------------------------------------------------------


class ChatSession:
    """一次 chat 会话的完整状态。"""

    def __init__(
        self,
        session_id: str,
        backend: str,
        budget: float,
        concurrency: int,
        fake: bool,
    ) -> None:
        self.session_id = session_id
        self.backend = backend
        self.budget = budget
        self.concurrency = concurrency
        self.fake = fake

        # 图存储 & WorldModel（跨任务复用）
        self.worldmodel: Any = None
        self._store: Any = None

        # 会话目标集
        self.targets: list[str] = []

        # 会话上下文（任务摘要，注入后续任务/问答）
        self.context: list[str] = []

        # 最近一次 engagement 报告
        self.last_report: Any = None

        # 工具注册表
        self.tools: Any = None

        # LLM
        self.llm: Any = None

    async def init(self) -> None:
        """初始化存储、LLM、工具。"""
        from secpt.config.models import GraphConfig
        from secpt.config.settings import get_settings
        from secpt.core.graph.factory import get_graph_store
        from secpt.core.graph.worldmodel import WorldModel
        from secpt.core.llm.factory import get_llm
        from secpt.core.tools.registry import build_registry

        settings = get_settings()

        # 图存储
        if self.backend == "sqlite":
            db_path = f"./var/sessions/{self.session_id}.db"
            graph_cfg = GraphConfig(backend="sqlite", sqlite_path=db_path)
        else:
            graph_cfg = GraphConfig(backend="memory")
        self._store = get_graph_store(graph_cfg)
        self.worldmodel = WorldModel(self._store)

        # LLM
        if self.fake:
            from secpt.core.llm import FakeLLM

            self.llm = FakeLLM(script=[])
        else:
            self.llm = get_llm(settings.llm)

        # 工具
        self.tools = build_registry(
            whitelist=settings.tools.command_whitelist,
            tool_names=settings.tools.tool_names,
        )

    async def close(self) -> None:
        if self._store is not None:
            await self._store.close()

    # ---- 任务派发 ----

    async def run_task(self, objective: str) -> Any:
        """把 objective 派发给 Engine（secpt.runtime.engine），返回完成摘要。

        不再走 EngagementRunner / Node 树 / 波前。
        """
        import tempfile
        from pathlib import Path as _P

        from secpt.core.profile.render import load_prior, render_system_extra
        from secpt.runtime import engine as _engine
        from secpt.runtime.llm.client import call_llm

        # 题面写入临时文件（保持与旧 harness 相同的落盘行为，便于调试）
        tmp = _P(tempfile.gettempdir()) / f"secpt_chat_{self.session_id}.md"
        tmp.write_text(objective, encoding="utf-8")

        job_id = f"chat_{self.session_id}"
        workspace = _P(tempfile.gettempdir()) / "secpt" / job_id

        cfg = _engine.RunConfig(
            job_id=job_id,
            objective=objective,
            system_extra="",
            workspace=workspace,
            flag_regex=_engine.DEFAULT_FLAG_REGEX,
            bash_whitelist=_engine.DEFAULT_BASH_WHITELIST,
        )
        cfg.system_extra = render_system_extra(
            "ctf",
            objective=objective,
            tools_doc=_engine.build_tools_doc(cfg),
            prior=load_prior("ctf"),
        )

        result = await _engine.Engine(cfg, llm=call_llm).run()
        flag = result.flag

        # 写入会话上下文
        if flag:
            self.context.append(f"任务完成，拿到 flag: {flag}")
        else:
            self.context.append(f"任务完成，未拿到 flag（{result.steps} 步）")

        return {
            "stdout": f"steps={result.steps} wall={result.wall_s:.1f}s status={result.status}",
            "stderr": "",
            "returncode": 0 if flag else 1,
            "flag": flag,
        }

    def _update_context(self, report: Any) -> None:
        """把本次 engagement 的关键发现压缩成摘要，追加进 session.context。"""
        summary_parts: list[str] = []

        # 节点终态
        done_nodes = [n for n in report.node_tree if n.status == "done"]
        dead_nodes = [n for n in report.node_tree if n.status == "dead"]
        summary_parts.append(
            f"任务完成：{len(report.node_tree)} 个节点（done={len(done_nodes)} dead={len(dead_nodes)}），"
            f"{report.total_wavefronts} 个波前"
        )

        # 关键证据（从 trajectory 提取）
        key_obs: list[str] = []
        for entries in report.trajectory.values():
            for e in entries:
                obs = e.observation
                if any(
                    kw in obs.lower()
                    for kw in (
                        "flag",
                        "rce",
                        "critical",
                        "漏洞",
                        "未授权",
                        "password",
                        "credential",
                    )
                ):
                    key_obs.append(obs[:120])
        if key_obs:
            summary_parts.append("关键发现：" + "; ".join(key_obs[:3]))

        # 图统计
        if report.graph_stats:
            last_gs = report.graph_stats[-1]
            summary_parts.append(
                f"图状态：{last_gs.entities} 实体 / {last_gs.edges} 边 / {last_gs.evidences} 证据"
            )

        # flag
        if report.evaluation and report.evaluation.flag_found:
            summary_parts.append(f"FLAG: {report.evaluation.flag_value}")

        self.context.append(" | ".join(summary_parts))

    # ---- 问答 ----

    async def classify_intent(self, text: str) -> str:
        """用 LLM 判断输入意图。返回 "task" / "question" / "command"。

        廉价调用：temperature=0，max_tokens=32。
        """
        import json as _json

        from secpt.core.llm.base import LLMMessage

        # / 开头一定是 command（不花 token）
        if text.startswith("/"):
            return "command"

        prompt = (
            f'判断以下输入的意图，只输出 JSON：{{"intent":"task"|"question"|"command"}}\n'
            f"- task: 要求执行渗透测试任务、扫描、攻击、解题\n"
            f"- question: 提问、询问信息、追问结果\n"
            f"- command: 系统命令（以 / 开头）\n"
            f"\n"
            f"输入：{text[:200]}\n"
        )

        messages = [
            LLMMessage(role="system", content="你是意图分类器。只输出 JSON。"),
            LLMMessage(role="user", content=prompt),
        ]

        try:
            resp = await self.llm.acompletion(
                messages,
                temperature=0,
                max_tokens=32,
            )
            content = (resp.content or "").strip()
            # 剥围栏
            content = (
                content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            )
            data = _json.loads(content)
            intent = data.get("intent", "task")
            if intent in ("task", "question", "command"):
                return intent
        except Exception:
            pass

        # 兜底：? 开头是 question，否则 task
        if text.startswith("?") or text.startswith("？"):
            return "question"
        return "task"

    async def ask(self, question: str) -> str:
        """基于当前 WorldModel + 会话上下文做一次 LLM 问答。"""
        from secpt.core.llm.base import LLMMessage

        # 组装上下文
        context_str = "\n".join(self.context) if self.context else "（暂无历史任务）"

        # WorldModel 摘要
        wm_summary = await self._graph_summary()

        prompt = (
            f"你是渗透测试分析助手。基于以下上下文回答用户问题。\n"
            f"\n"
            f"=== 会话上下文 ===\n{context_str}\n"
            f"\n"
            f"=== 当前图状态 ===\n{wm_summary}\n"
            f"\n"
            f"=== 用户问题 ===\n{question}\n"
        )

        messages = [
            LLMMessage(role="system", content="你是渗透测试分析助手，基于给定上下文回答问题。"),
            LLMMessage(role="user", content=prompt),
        ]

        resp = await self.llm.acompletion(messages)
        return (resp.content or "").strip()

    async def _graph_summary(self) -> str:
        """获取 WorldModel 统计摘要。"""
        store = self.worldmodel._store

        # MemoryGraphStore
        if hasattr(store, "_entities"):
            entities = len(store._entities)
            edges = sum(len(v) for v in store._edges.values()) if hasattr(store, "_edges") else 0
            evidences = len(store._evidences) if hasattr(store, "_evidences") else 0
            return f"实体数: {entities}, 边数: {edges}, 证据数: {evidences}"

        # SQLiteGraphStore
        if hasattr(store, "_conn") and store._conn is not None:
            try:
                entities = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
                edges = store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                evidences = store._conn.execute("SELECT COUNT(*) FROM evidences").fetchone()[0]
                return f"实体数: {entities}, 边数: {edges}, 证据数: {evidences}"
            except Exception:
                pass

        return "（无法获取图统计）"

    async def graph_stats(self) -> dict:
        """获取详细图统计（含 top5 高分实体）。"""
        store = self.worldmodel._store
        result: dict[str, Any] = {"entities": 0, "edges": 0, "evidences": 0, "top5": []}

        if hasattr(store, "_entities"):
            result["entities"] = len(store._entities)
            result["edges"] = (
                sum(len(v) for v in store._edges.values()) if hasattr(store, "_edges") else 0
            )
            result["evidences"] = len(store._evidences) if hasattr(store, "_evidences") else 0
            # top5（内存后端简化为前 5 个）
            for eid, ent in list(store._entities.items())[:5]:
                result["top5"].append(f"  {eid} ({ent.get('etype', 'unknown')})")

        elif hasattr(store, "_conn") and store._conn is not None:
            try:
                result["entities"] = store._conn.execute(
                    "SELECT COUNT(*) FROM entities"
                ).fetchone()[0]
                result["edges"] = store._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                result["evidences"] = store._conn.execute(
                    "SELECT COUNT(*) FROM evidences"
                ).fetchone()[0]
                top = store._conn.execute("SELECT eid, etype FROM entities LIMIT 5").fetchall()
                for row in top:
                    result["top5"].append(f"  {row[0]} ({row[1]})")
            except Exception:
                pass

        return result


# ---------------------------------------------------------------------------
# REPL 输入（prompt_toolkit 可选）
# ---------------------------------------------------------------------------


def _make_input_fn():
    """返回异步输入函数。优先 prompt_toolkit，否则退化到 input()。"""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        history_dir = Path.home() / ".secpt"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_path = str(history_dir / "history")

        ptk_session = PromptSession(history=FileHistory(history_path))

        async def _ptk_input(prompt: str) -> str:
            return await asyncio.to_thread(ptk_session.prompt, prompt)

        return _ptk_input
    except ImportError:
        pass

    async def _builtin_input(prompt: str) -> str:
        return await asyncio.to_thread(input, prompt)

    return _builtin_input


# ---------------------------------------------------------------------------
# 命令处理
# ---------------------------------------------------------------------------

HELP_TEXT = """\
可用命令：
  /tools                 列出已注册工具
  /targets [host...]     查看或设置当前会话目标集
  /graph                 打印 WorldModel 统计
  /status                最近一次 engagement 摘要
  /ask <问题>            等价于 ? 开头的问答
  /report <path>         导出最近一次报告为 JSON
  /help                  打印帮助
  /exit 或 /quit         退出

输入规则：
  / 开头 → 内置命令
  ? 开头 → 问答
  其它   → 任务派发（自然语言即任务）
"""


async def _cmd_tools(session: ChatSession) -> str:
    """列出已注册工具。"""
    lines = ["已注册工具:"]
    for t in session.tools.all():
        lines.append(f"  {t.name:20s} {t.description[:60]}")
    return "\n".join(lines)


async def _cmd_targets(session: ChatSession, args: list[str]) -> str:
    """查看或设置目标集。"""
    if args:
        session.targets = args
        return f"目标集已设置: {session.targets}"
    if session.targets:
        return f"当前目标: {session.targets}"
    return "当前无目标（用 /targets host1 host2 设置）"


async def _cmd_graph(session: ChatSession) -> str:
    """打印 WorldModel 统计。"""
    stats = await session.graph_stats()
    lines = [
        f"实体数: {stats['entities']}",
        f"边数:   {stats['edges']}",
        f"证据数: {stats['evidences']}",
    ]
    if stats["top5"]:
        lines.append("高分实体 top5:")
        lines.extend(stats["top5"])
    return "\n".join(lines)


async def _cmd_status(session: ChatSession) -> str:
    """最近一次 engagement 摘要。"""
    if session.last_report is None:
        return "尚未执行过任务"
    r = session.last_report
    flag_str = "✓" if r.evaluation and r.evaluation.flag_found else "✗"
    return (
        f"最近一次 engagement:\n"
        f"  波前数: {r.total_wavefronts}\n"
        f"  节点数: {len(r.node_tree)}\n"
        f"  耗时:   {r.duration_s:.2f}s\n"
        f"  FLAG:   {flag_str}"
    )


async def _cmd_report(session: ChatSession, args: list[str]) -> str:
    """导出报告为 JSON。"""
    if session.last_report is None:
        return "尚无报告可导出"
    if not args:
        return "用法: /report <path>"
    path = args[0]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(session.last_report.model_dump_json(indent=2))
    return f"报告已写入: {path}"


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


async def _run_repl(session: ChatSession) -> None:
    """主 REPL 循环。"""
    input_fn = _make_input_fn()

    # 欢迎信息
    tool_count = len(session.tools.all())
    backend_str = session.backend
    print("secpt chat 已启动")
    print(f"  会话 ID:  {session.session_id}")
    print(f"  后端:     {backend_str}")
    print(f"  工具数:   {tool_count}")
    print("  输入 /help 查看帮助，/exit 退出")
    print()

    while True:
        try:
            user_input = await input_fn("secpt> ")
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D / Ctrl-C 在输入阶段 → 退出
            print("\n再见!")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # 分派：先 LLM 判意图，? 和 / 前缀降级为兜底
        try:
            # 硬规则优先（不花 token）
            if user_input.startswith("/"):
                should_exit = await _handle_command(session, user_input)
                if should_exit:
                    break
                continue

            # LLM 意图判断
            intent = await session.classify_intent(user_input)

            if intent == "question":
                # 去掉 ? 前缀（如果有）
                question = user_input.lstrip("?？ ").strip()
                answer = await session.ask(question)
                print(f"\n{answer}\n")
            elif intent == "command":
                # 当作命令处理
                should_exit = await _handle_command(session, user_input)
                if should_exit:
                    break
            else:
                # task
                await _handle_task(session, user_input)
        except asyncio.CancelledError:
            print("\n[已中断]")
        except KeyboardInterrupt:
            print("\n[已中断当前任务]")
        except Exception as exc:
            logger.error("chat.error", error=str(exc))
            print(f"Error: {exc}")


async def _handle_command(session: ChatSession, user_input: str) -> bool:
    """处理内置命令。返回 True 表示应退出 REPL。"""
    parts = user_input.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/exit", "/quit"):
        print("再见!")
        return True

    if cmd == "/help":
        print(HELP_TEXT)
    elif cmd == "/tools":
        print(await _cmd_tools(session))
    elif cmd == "/targets":
        print(await _cmd_targets(session, args))
    elif cmd == "/graph":
        print(await _cmd_graph(session))
    elif cmd == "/status":
        print(await _cmd_status(session))
    elif cmd == "/ask":
        if not args:
            print("用法: /ask <问题>")
        else:
            question = " ".join(args)
            answer = await session.ask(question)
            print(f"\n{answer}\n")
    elif cmd == "/report":
        print(await _cmd_report(session, args))
    else:
        print(f"未知命令: {cmd}（/help 查看帮助）")

    return False


async def _handle_task(session: ChatSession, objective: str) -> None:
    """处理自然语言任务派发：走 Engine。"""
    print(f"[任务] {objective[:80]}")

    try:
        result = await session.run_task(objective)
    except asyncio.CancelledError:
        print("[任务已中断]")
        raise

    print(result["stdout"])

    if result["stderr"]:
        print(f"[stderr] {result['stderr'][:500]}")

    if result.get("flag"):
        print(f"\n[FLAG] {result['flag']}\n")
        # 写入上下文
        session.context.append(f"任务完成，拿到 flag: {result['flag']}")
    else:
        print(f"\n[完成] returncode={result['returncode']}，未拿到 flag\n")


def _setup_fake_script(session: ChatSession, objective: str) -> None:
    """为 FakeLLM 设置脚本（根据目标数量动态生成）。"""
    from secpt.core.llm import FakeLLM, LLMMessage

    target = session.targets[0] if session.targets else "target"
    script = [
        LLMMessage(role="assistant", content="HYPOTHESIS: 分析目标"),
        LLMMessage(role="assistant", content=f"ACTION: crawler http://{target}"),
        LLMMessage(role="assistant", content="HYPOTHESIS: 发现漏洞"),
        LLMMessage(role="assistant", content=f"ACTION: flag_extract http://{target}"),
        LLMMessage(role="assistant", content="ANSWER: FLAG{fake_flag_from_task}"),
    ]
    session.llm = FakeLLM(script=script)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def chat_cmd(
    backend: str = typer.Option("memory", "--backend", help="GraphStore 后端 (memory/sqlite)"),
    budget: float = typer.Option(50.0, "--budget", "-b", help="总预算"),
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="最大并发"),
    session_id: str | None = typer.Option(None, "--session", "-s", help="会话 ID（默认自动生成）"),
    fake: bool = typer.Option(False, "--fake", "-f", help="FakeLLM 干跑"),
) -> None:
    """进入交互式会话（自然语言即任务）。"""

    sid = session_id or f"sess.{uuid.uuid4().hex[:8]}"

    async def _main():
        session = ChatSession(
            session_id=sid,
            backend=backend,
            budget=budget,
            concurrency=concurrency,
            fake=fake,
        )
        await session.init()
        try:
            await _run_repl(session)
        finally:
            await session.close()

    asyncio.run(_main())


__all__ = ["ChatSession", "chat_cmd"]
